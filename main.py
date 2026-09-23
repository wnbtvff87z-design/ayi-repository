import os
import json
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from twilio.rest import Client as TwilioClient
from openai import OpenAI
import stripe
import requests

# Load environment variables
load_dotenv()

# Initialize Flask
app = Flask(__name__)

# Initialize Twilio
twilio_client = TwilioClient(
    os.getenv('TWILIO_ACCOUNT_SID'),
    os.getenv('TWILIO_AUTH_TOKEN')
)
twilio_phone = os.getenv('TWILIO_PHONE')

# Initialize OpenAI (MODELO BARATO)
openai_client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

# Initialize Stripe
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')

# Airtable config
AIRTABLE_TOKEN = os.getenv('AIRTABLE_TOKEN')
AIRTABLE_BASE_ID = os.getenv('AIRTABLE_BASE_ID')
AIRTABLE_RESTAURANTS_TABLE = "Restaurantes"  # Para leer
AIRTABLE_CONVERSATIONS_TABLE = "Conversaciones"  # Para guardar
AIRTABLE_API_URL_BASE = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}"


# ============================================================================
# AIRTABLE FUNCTIONS
# ============================================================================

def get_restaurant_data(phone_number):
    """Busca restaurante por número Twilio en Airtable"""
    headers = {
        'Authorization': f'Bearer {AIRTABLE_TOKEN}',
        'Content-Type': 'application/json'
    }
    
    try:
        url = f"{AIRTABLE_API_URL_BASE}/{AIRTABLE_RESTAURANTS_TABLE}"
        response = requests.get(
            url,
            headers=headers,
            params={'filterByFormula': f'{{Twilio_Phone}}="{phone_number}"'}
        )
        
        if response.status_code == 200:
            records = response.json().get('records', [])
            if records:
                return records[0]['fields']
    except Exception as e:
        print(f"❌ Error getting restaurant data: {e}")
    
    return None


def save_message_to_airtable(phone_number, customer_number, question, answer):
    """Guarda pregunta/respuesta en tabla Conversaciones"""
    headers = {
        'Authorization': f'Bearer {AIRTABLE_TOKEN}',
        'Content-Type': 'application/json'
    }
    
    data = {
        'records': [
            {
                'fields': {
                    'Twilio_Phone': phone_number,
                    'Customer_Phone': customer_number,
                    'Question': question,
                    'Answer': answer,
                    'Timestamp': datetime.now().isoformat(),
                    'Status': 'Answered by AI'
                }
            }
        ]
    }
    
    try:
        url = f"{AIRTABLE_API_URL_BASE}/{AIRTABLE_CONVERSATIONS_TABLE}"
        response = requests.post(url, headers=headers, json=data)
        if response.status_code == 200:
            print(f"✅ Saved to Airtable: {question} → {answer}")
        else:
            print(f"❌ Airtable error: {response.status_code}")
    except Exception as e:
        print(f"❌ Error saving to Airtable: {e}")


# ============================================================================
# OPENAI FUNCTION (gpt-3.5-turbo - BARATO)
# ============================================================================

def get_ai_response(question, restaurant_data):
    """Conecta con OpenAI gpt-3.5-turbo (BARATO)"""
    
    restaurant_name = restaurant_data.get('Nombre_Restaurante', 'Restaurant')
    menu = restaurant_data.get('Menu', 'No menu available')
    hours = restaurant_data.get('Horarios', 'Call for hours')
    
    system_prompt = f"""You are a professional restaurant assistant for '{restaurant_name}'.

Restaurant Info:
- Name: {restaurant_name}
- Hours: {hours}
- Menu: {menu}

Instructions:
1. Answer questions about reservations, hours, menu, or general info
2. Be professional and concise (max 2 sentences)
3. If asked about reservations, collect: name, time, number of people
4. Always respond in Spanish
5. If unsure, suggest calling the restaurant directly"""

    try:
        response = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",  # ← CAMBIO: modelo barato
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question}
            ],
            max_tokens=150,
            temperature=0.7
        )
        
        return response.choices[0].message.content
    
    except Exception as e:
        print(f"❌ OpenAI Error: {e}")
        return "Lo siento, no pude procesar tu pregunta. Por favor llama directamente."


# ============================================================================
# TWILIO WEBHOOK - RECIBE WHATSAPP
# ============================================================================

@app.route('/webhook-whatsapp', methods=['POST'])
def webhook_whatsapp():
    """Recibe mensajes de Twilio WhatsApp"""
    
    print("📨 Webhook recibido")
    
    # Extract incoming message
    incoming_phone = request.form.get('From')
    incoming_message = request.form.get('Body')
    twilio_number = request.form.get('To')
    
    print(f"📨 From: {incoming_phone}, To: {twilio_number}, Message: {incoming_message}")
    
    # Get restaurant data from Airtable
    restaurant = get_restaurant_data(twilio_number)
    
    if not restaurant:
        print(f"❌ No restaurant found for {twilio_number}")
        response_text = "No encontramos tu restaurante. Contacta al soporte."
        twilio_client.messages.create(
            from_=twilio_phone,
            to=incoming_phone,
            body=response_text
        )
        return jsonify({'status': 'error', 'message': 'Restaurant not found'}), 404
    
    print(f"✅ Found restaurant: {restaurant.get('Nombre_Restaurante')}")
    
    # Get AI response from OpenAI
    ai_response = get_ai_response(incoming_message, restaurant)
    
    # Save to Airtable
    save_message_to_airtable(
        twilio_number,
        incoming_phone,
        incoming_message,
        ai_response
    )
    
    # Send response back via WhatsApp
    twilio_client.messages.create(
        from_=twilio_phone,
        to=incoming_phone,
        body=ai_response
    )
    
    print(f"✅ Response sent: {ai_response}")
    
    return jsonify({'status': 'success'})


# ============================================================================
# HEALTH CHECK
# ============================================================================

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'OK', 'timestamp': datetime.now().isoformat()})

@app.route('/', methods=['GET'])
def home():
    return jsonify({
        'name': 'Ayi Reservas API',
        'status': 'running',
        'version': '1.0.0'
    })


# ============================================================================
# RUN
# ============================================================================

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
