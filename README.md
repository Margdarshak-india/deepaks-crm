# Deepak's CRM V2

Standalone WhatsApp CRM prototype.

## Run
1. Install Python 3.10+.
2. `pip install -r requirements.txt`
3. Set `WHATSAPP_ACCESS_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID`.
4. `python app.py`
5. Open http://127.0.0.1:5000

V2 adds an initial official WhatsApp Cloud API sending layer. Before production, add approved template management, delivery/read/failure webhooks, opt-out list, recipient logs, rate limiting, authentication and secure secrets.
