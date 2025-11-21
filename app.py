import os
import re
import io
import requests
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from flask import Flask, request, Response
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

# Google Cloud Imports
from google.cloud import speech
import gspread
from gspread import Worksheet

# --- Configuration & Initialization ---

app = Flask(__name__)

# CONFIGURATION VARIABLES - Set these as Environment Variables on Render!
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS_PATH")

# **UPDATED WITH YOUR SHEET DETAILS**
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID") 
# Assuming the first tab is named 'Sheet1' (a common default)
WORKSHEET_NAME = "Sheet1" 

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# **STATE MANAGEMENT**: A simple, temporary in-memory store for pending transcriptions.
PENDING_TRANSCRIPTIONS: Dict[str, str] = {} 

# --- Google Sheets and STT Setup ---

def setup_google_sheets() -> Worksheet:
    """Authenticates and returns the target Google Sheet worksheet."""
    try:
        # Authentication using the service account file
        gc = gspread.service_account(filename=GOOGLE_CREDENTIALS_PATH)
        # Open by ID
        sh = gc.open_by_key(GOOGLE_SHEET_ID) 
        # Attempt to open the specific worksheet (tab)
        worksheet = sh.worksheet(WORKSHEET_NAME) 
        return worksheet
    except Exception as e:
        print(f"Error setting up Google Sheets. Make sure the Service Account is shared with the sheet: {e}")
        raise 

# Initialize the worksheet once
try:
    import gspread 
    SHEET = setup_google_sheets()
except ImportError:
    print("WARNING: gspread not installed. Cannot initialize Google Sheets.")
    SHEET = None
except Exception:
    print("WARNING: Could not connect to Google Sheet. Check credentials/ID/WORKSHEET_NAME.")
    SHEET = None

def setup_google_stt_client() -> speech.SpeechClient:
    """Authenticates and returns the Google Speech Client."""
    try:
        import google.cloud.speech 
        stt_client = speech.SpeechClient()
        return stt_client
    except ImportError:
        print("WARNING: google-cloud-speech not installed. STT will fail.")
        return None
    except Exception as e:
        print(f"Error setting up Google STT Client: {e}")
        raise

STT_CLIENT = setup_google_stt_client()

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
        r"(?:.*notes\s+(?P<notes>.*))?",                                       # Notes (captures the rest)
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
        speech_context=[speech.SpeechContext(phrases=PHRASE_HINTS)]
    )
    
    response = STT_CLIENT.recognize(config=config, audio=audio)
    
    if response.results:
        return response.results[0].alternatives[0].transcript
    return ""

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
                delivery_data['date'] = datetime.now().strftime('%Y-%m-%d')
                delivery_data['phone_number'] = from_number
                delivery_data['reminders'] = calculate_reminders(datetime.now())
                
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
            audio_response = requests.get(media_url)
            audio_response.raise_for_status() 
            audio_bytes = audio_response.content
        except requests.exceptions.RequestException:
            resp.message("❌ ERROR: Could not download the voice message. Check Twilio settings or the media URL.")
            return Response(str(resp), mimetype='application/xml')

        if STT_CLIENT:
            transcribed_text = transcribe_audio_file(audio_bytes)
        else:
            transcribed_text = "" 
        
        if transcribed_text:
            PENDING_TRANSCRIPTIONS[from_number] = transcribed_text
            
            response_msg = (
                f"I heard: **{transcribed_text}**\n\n"
                "To confirm this transcription and fill the database, **REPLY WITH 1**."
            )
            resp.message(response_msg)
        else:
            resp.message("Sorry, I could not transcribe the voice message. Ensure you have properly set up the Google STT credentials.")
            
        return Response(str(resp), mimetype='application/xml')

    # --- Default Text Handler ---
    else:
        resp.message("Welcome! Please send a voice note with the delivery details, or reply '1' to confirm a pending transcription.")
        return Response(str(resp), mimetype='application/xml')
    
if __name__ == '__main__':

    app.run(debug=True)
