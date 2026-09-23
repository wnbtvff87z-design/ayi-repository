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

# Initialize OpenAI (GPT-6 Luna)
openai_client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

# Initialize Stripe
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')

# Airtable config
AIRTABLE_TOKEN = os.getenv('AIRTABLE_TOKEN')
AIRTABLE_BASE_ID = os.getenv('AIRTABLE_BASE_ID')
AIRTABLE_TABLE_NAME = os.getenv('AIRTABLE_TABLE_NAME')
AIRTABLE_API_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"


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
        response = requests.get(
            AIRTABLE_API_URL,
            headers=headers,
            params={'filterByFormula': f'{{Twilio_Phone}}="{phone_number}"'}
        )
        
        if response.status_code == 200:
            records = response.json().get('records', [])
            if records:
                return records[0]['fields']
    except Exception as e:
        print(f"Error getting restaurant data: {e}")
    
    return None


def save_message_to_airtable(phone_number, customer_number, question, answer):
    """Guarda pregunta/respuesta en Airtable"""
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
        requests.post(AIRTABLE_API_URL, headers=headers, json=data)
    except Exception as e:
        print(f"Error saving to Airtable: {e}")


# ============================================================================
# OPENAI FUNCTION (GPT-6 Luna)
# ============================================================================

def get_ai_response(question, restaurant_data):
    """Conecta con OpenAI GPT-6 Luna para generar respuesta"""
    
    # Build context from restaurant data
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
4. Always be polite in Spanish
5. If unsure, suggest calling the restaurant directly

Respond ONLY with the answer, no preamble."""

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4-turbo",  # Usa gpt-4-turbo si Luna no está disponible
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question}
            ],
            max_tokens=150,
            temperature=0.7
        )
        
        return response.choices[0].message.content
    
    except Exception as e:
        print(f"OpenAI Error: {e}")
        return "Lo siento, no pude procesar tu pregunta. Por favor llama directamente."


# ============================================================================
# TWILIO WEBHOOK - RECIBE WHATSAPP
# ============================================================================

@app.route('/webhook-whatsapp', methods=['POST'])
def webhook_whatsapp():
    """Recibe mensajes de Twilio WhatsApp"""
    
    # Extract incoming message
    incoming_phone = request.form.get('From')  # +34 666 123 456
    incoming_message = request.form.get('Body')  # "¿Horarios?"
    twilio_number = request.form.get('To')  # +34 91 234 5678 (tu número)
    
    print(f"📨 Message from {incoming_phone}: {incoming_message}")
    
    # Get restaurant data from Airtable
    restaurant = get_restaurant_data(twilio_number)
    
    if not restaurant:
        response_text = "No encontramos tu restaurante en nuestro sistema. Contacta al soporte."
        twilio_client.messages.create(
            from_=twilio_phone,
            to=incoming_phone,
            body=response_text
        )
        return jsonify({'status': 'error', 'message': 'Restaurant not found'})
    
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
# STRIPE WEBHOOK - PAGOS
# ============================================================================

@app.route('/webhook-stripe', methods=['POST'])
def webhook_stripe():
    """Webhook para pagos recurrentes de Stripe"""
    
    sig_header = request.headers.get('Stripe-Signature')
    event = None
    
    try:
        event = stripe.Webhook.construct_event(
            request.data,
            sig_header,
            os.getenv('STRIPE_WEBHOOK_SECRET')
        )
    except ValueError:
        return jsonify({'error': 'Invalid payload'}), 400
    except stripe.error.SignatureVerificationError:
        return jsonify({'error': 'Invalid signature'}), 400
    
    # Handle different event types
    if event['type'] == 'customer.subscription.created':
        print(f"✅ New subscription: {event['data']['object']['id']}")
        # Aquí puedes activar la suscripción en Airtable
    
    elif event['type'] == 'customer.subscription.deleted':
        print(f"❌ Subscription cancelled: {event['data']['object']['id']}")
        # Aquí puedes desactivar la suscripción en Airtable
    
    elif event['type'] == 'invoice.payment_failed':
        print(f"⚠️ Payment failed: {event['data']['object']['id']}")
        # Aquí puedes enviar email de alerta
    
    return jsonify({'status': 'received'})


# ============================================================================
# HEALTH CHECK
# ============================================================================

@app.route('/health', methods=['GET'])
def health():
    """Health check para Railway"""
    return jsonify({'status': 'OK', 'timestamp': datetime.now().isoformat()})


@app.route('/', methods=['GET'])
def home():
    """Home page"""
    return jsonify({
        'name': 'Ayi Reservas API',
        'status': 'running',
        'version': '1.0.0',
        'endpoints': {
            'webhook_whatsapp': '/webhook-whatsapp',
            'webhook_stripe': '/webhook-stripe',
            'health': '/health'
        }
    })


# ============================================================================
# RUN
# ============================================================================

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
