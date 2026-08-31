# Margdarshak's CRM

Updated Flask WhatsApp CRM package.

## Render
- Runtime: Python
- Start command is supplied by `Procfile`: `web: gunicorn app:app`
- Keep your existing Render environment variables unchanged.
- The UI changes do not create or replace secrets.

## Navigation
The main navigation is a hamburger drawer. Google Drive and Webhook Logs remain available by route but are intentionally removed from the main toolbar.

## Theme
Light/Dark selection is stored in the browser with localStorage.
