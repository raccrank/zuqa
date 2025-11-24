import os
import re
import io
import requests
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import traceback # NEW: Import traceback for detailed error logging

from flask import Flask, request, Response
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

# Google Cloud Imports
from google.cloud import speech
import gspread
from gspread import Worksheet

# --- Configuration & Initialization ---

app = Flask(__name__)

# CONFIGURATION VARIABLES
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
# NOTE: Using 'secrets' vs 'secret' folder. Ensure this path is correct on Render!
GOOGLE_CREDENTIALS_PATH = os.environ.get("GOOGLE_CREDENTIALS_PATH", "/etc/secrets/sonorous-study.json") 

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID") 
WORKSHEET_NAME = "Sheet1" 

# NEW: Safely initialize the Twilio Client globally
try:
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
except Exception as e:
    print(f"WARNING: Twilio Client initialization failed: {e}")
    client = None

# **STATE MANAGEMENT**: A simple, temporary in-memory store for pending transcriptions.
PENDING_TRANSCRIPTIONS: Dict[str, str] = {} 

# --- Google Sheets and STT Setup ---

def setup_google_sheets() -> Worksheet:
    """Authenticates and returns the target Google Sheet worksheet."""
    try:
        # gspread.service_account(filename=...) is correct for file path authentication
        gc = gspread.service_account(filename=GOOGLE_CREDENTIALS_PATH)
        sh = gc.open_by_key(GOOGLE_SHEET_ID) 
        worksheet = sh.worksheet(WORKSHEET_NAME) 
        return worksheet
    except Exception as e:
        print(f"Error setting up Google Sheets. Make sure the Service Account is shared with the sheet: {e}")
        # Re-raise the exception to maintain the original error behavior
        raise 

# Initialize the worksheet once
try:
    import gspread 
    SHEET = setup_google_sheets()
except ImportError:
    print("WARNING: gspread not installed. Cannot initialize Google Sheets.")
    SHEET = None
except Exception:
    # This catch block is where the gspread setup error occurred in your logs.
    # The fix ensures the `raise` inside `setup_google_sheets` works correctly.
    print("WARNING: Could not connect to Google Sheet. Check credentials/ID/WORKSHEET_NAME.")
    SHEET = None

def setup_google_stt_client() -> speech.SpeechClient:
    """Authenticates and returns the Google Speech Client."""
    try:
        import google.cloud.speech 
        # Explicitly authenticate using the JSON file path
        stt_client = speech.SpeechClient.from_service_account_json(GOOGLE_CREDENTIALS_PATH)
        return stt_client
    except ImportError:
        print("WARNING: google-cloud-speech not installed. STT will fail.")
        return None
    except Exception as e:
        # This will now catch the STT authentication error if the file is invalid/inaccessible
        print(f"Error setting up Google STT Client: {e}")
        # We re-raise the error so the main process can handle the failure during startup
        raise 

# Initialize the STT Client once globally, using the safe function.
try:
    STT_CLIENT = setup_google_stt_client()
except Exception:
    # If the setup function fails and re-raises, catch it here so the Flask app can still start 
    # (though transcription will be disabled). We log the failure clearly.
    print("FATAL: Google STT Client initialization failed during startup. Check credentials file path.")
    STT_CLIENT = None


# --- Vaccination and Parsing Logic ---

def calculate_reminders(delivery_date: datetime) -> str:
    """Calculates vaccination reminder dates."""
    guboro_date = delivery_date + timedelta(days=14)
    lasota_date = delivery_date + timedelta(days=21)
    
    return (
        f"Guboro: {guboro_date.strftime('%Y-%m-%d')}; "
        f"La Sota: {lasota_date.strftime('%Y-%m-%d')}"
    )

def parse_delivery_transcription(transcription: str) -> Optional[Dict[str, Any]]:
    """
    Parses key fields from the transcribed text.
    Uses regex to extract required fields, prioritizing numbers.
    """
    
    pattern = re.compile(
        # Client Index (1-7)
        r".*client\s+(?P<client_index>[1-7])"                     
        # Quantity and Feed Type (must capture the feed item)
        r".*delivered\s+(?P<quantity>\d+)\s+(?P<feed_type>crumbs|pellets|day old chicks|layer mash)(?:\s+at)?"
        r".*price\s+(?P<price>\d+)"                                     
        # Location (constrained to your list)
        r"(?:.*location\s+(?P<location>matangi|kitengela|mihang'o)\s*)"    
        r"(?:.*notes\s+(?P<notes>.*))?",                              # Notes (captures the rest)
        re.IGNORECASE | re.DOTALL
    )
    
    match = pattern.search(transcription)
    
    if match:
        data = match.groupdict()
        # Clean up and normalize data
        data['debt'] = int(data.get('debt', 0) or 0)
        data['overpaid'] = int(data.get('overpaid', 0) or 0)
        
        try:
            data['quantity'] = int(data.get('quantity'))
            data['price'] = int(data.get('price')) 
        except (ValueError, TypeError):
             return None 

        data['client_index'] = data.get('client_index', 'N/A').strip()
        data['feed_type'] = data.get('feed_type', 'N/A').strip() 
        data['location'] = data.get('location', 'N/A').strip()
        data['notes'] = data.get('notes', 'N/A').strip()
        
        return data
    return None

def transcribe_audio_file(audio_bytes: bytes) -> str:
    """Sends audio bytes to Google Cloud Speech-to-Text for transcription."""
    
    if not STT_CLIENT:
        return "STT_CLIENT is unavailable."

    # Custom Vocabulary using your specific terms
    PHRASE_HINTS = [
        "crumbs", "pellets", "day old chicks", "layer mash", 
        "debt", "overpaid", "client", "price", "location", 
        "matangi", "kitengela", "mihang'o", 
        "one", "two", "three", "four", "five", "six", "seven", 
        "500", "1000", "2000", "1200", "delivered"
    ]
    
    audio = speech.RecognitionAudio(content=audio_bytes)
    
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.OGG_OPUS, 
        sample_rate_hertz=16000,
        language_code="en-US", 
        # FIX: Changed 'speech_context' to the correct plural form 'speech_contexts'
        speech_contexts=[speech.SpeechContext(phrases=PHRASE_HINTS)] 
    )
    
    try:
        response = STT_CLIENT.recognize(config=config, audio=audio)
        if response.results:
            return response.results[0].alternatives[0].transcript
        return ""
    except Exception as e:
        print(f"ERROR during Google STT recognition: {e}")
        return f"Transcription failed: {e}"


# --- Google Sheets Logging ---

def log_to_google_sheet(data: Dict[str, Any]) -> bool:
    """Appends the parsed delivery data to the Google Sheet."""
    if not SHEET:
        return False
        
    try:
        # Log columns: Date, Client Phone Number, Client Index, Quantity, Feed, Price, Debt, Overpaid, Location, Notes, Reminders
        row_data = [
            data['date'],
            data['phone_number'],
            data['client_index'],
            data['quantity'],
            data['feed_type'],
            data['price'],
            data['location'],
            data['notes'],
            data['reminders']
        ]
        
        SHEET.append_row(row_data)
        return True
    except Exception as e:
        print(f"Error appending data to Google Sheet: {e}")
        return False


# --- Twilio Webhook Logic ---

@app.route("/whatsapp", methods=['GET', 'POST'])
def whatsapp_reply():
    """Handles incoming WhatsApp messages for the two-step logging process."""
    
    # NEW: Top-level error handler to catch *any* unhandled exception and log the traceback
    try:
        resp = MessagingResponse()
        from_number = request.values.get('From', '').replace('whatsapp:', '')
        incoming_text = request.values.get('Body', '').strip()
        num_media = int(request.values.get('NumMedia', 0))

        # --- PHASE 2: Confirmation / Logging ---
        if incoming_text == '1':
            if from_number in PENDING_TRANSCRIPTIONS:
                transcription = PENDING_TRANSCRIPTIONS.pop(from_number)
                delivery_data = parse_delivery_transcription(transcription)

                if delivery_data:
                    # Calculate the date and reminders based on the current time
                    delivery_date = datetime.now()
                    delivery_data['date'] = delivery_date.strftime('%Y-%m-%d')
                    delivery_data['phone_number'] = from_number
                    delivery_data['reminders'] = calculate_reminders(delivery_date)
                    
                    if log_to_google_sheet(delivery_data):
                        resp.message("✅ Database filled! Delivery details have been successfully logged to the Google Sheet, and reminders calculated.")
                    else:
                        resp.message("❌ ERROR: Failed to log data to the Google Sheet. Ensure your sheet has the tab name 'Sheet1' (or change the variable) and the service account has edit access.")
                else:
                    resp.message(f"❌ ERROR: The transcription could not be parsed into fields. Please ensure the voice note follows the expected format. Transcription received: {transcription}")
            else:
                resp.message("I didn't find any pending transcription to confirm. Please send a voice note first.")
            
            return Response(str(resp), mimetype='application/xml')

        # --- PHASE 1: Voice Note Transcription ---
        elif num_media > 0 and request.values.get('MediaContentType0', '').startswith('audio'):
            
            media_url = request.values.get('MediaUrl0')
            
            try:
                # Check Twilio credentials before making the request
                if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
                    raise ValueError("Twilio credentials are not loaded from environment variables.")

                audio_response = requests.get(
                    media_url, 
                    auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), 
                    timeout=10
                )
                audio_response.raise_for_status() 
                audio_bytes = audio_response.content
            except requests.exceptions.RequestException as e:
                # Log the specific request error for detailed debugging on Render
                print(f"REQUESTS ERROR downloading media: {e}") 
                resp.message("❌ ERROR: Could not download the voice message. Check Twilio settings and ensure your credentials are correct.")
                return Response(str(resp), mimetype='application/xml')
            except ValueError as e:
                print(f"CONFIGURATION ERROR: {e}")
                resp.message("❌ CONFIGURATION ERROR: Twilio credentials not found. Check environment variables.")
                return Response(str(resp), mimetype='application/xml')

            if STT_CLIENT:
                transcribed_text = transcribe_audio_file(audio_bytes)
            else:
                # Handle the case where the STT client failed to initialize globally
                transcribed_text = ""
                resp.message("Sorry, the transcription service is currently unavailable due to a configuration error. Please check server logs.")
                print("STT_CLIENT is None. Check setup_google_stt_client logs for details.")
                return Response(str(resp), mimetype='application/xml') # Early exit on critical error
            
            if transcribed_text and not transcribed_text.startswith("Transcription failed") and transcribed_text != "No transcription results found.":
                PENDING_TRANSCRIPTIONS[from_number] = transcribed_text
                
                response_msg = (
                    f"I heard: **{transcribed_text}**\n\n"
                    "To confirm this transcription and fill the database, **REPLY WITH 1**."
                )
                resp.message(response_msg)
            else:
                # If STT failed to transcribe (e.g., error from Google API)
                print(f"Transcription failed or found no results. Output: {transcribed_text}")
                resp.message("Sorry, I could not transcribe the voice message. Ensure the audio is clear and you have properly set up the Google STT credentials.")
                
            return Response(str(resp), mimetype='application/xml')

        # --- Default Text Handler ---
        else:
            resp.message("Welcome! Please send a voice note with the delivery details, or reply '1' to confirm a pending transcription.")
            return Response(str(resp), mimetype='application/xml')
            
    except Exception as e:
        # This catches any remaining unhandled error in the function
        print(f"CRITICAL UNHANDLED ERROR processing WhatsApp message: {e}")
        # Print the full traceback to the Render logs
        print(traceback.format_exc())
        
        # Return a controlled error message to Twilio
        resp_error = MessagingResponse()
        resp_error.message("❌ FATAL ERROR: An unexpected server error occurred. The error has been logged for debugging. Please check your Render logs for the full Python traceback.")
        return Response(str(resp_error), mimetype='application/xml', status=500)

if __name__ == '__main__':

    app.run(debug=True)
