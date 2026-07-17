# Kommo Private Salesbot Widget

This widget adds a Salesbot designer block that calls `https://<store-domain>/webhooks/kommo/salesbot` through Kommo `widget_request`.

Build:

```bash
cd store/kommo-widget
python3 build_widget.py
```

Upload `social-media-manager-kommo-widget.zip` to the private Kommo integration. The ZIP places `manifest.json` at the archive root.

Salesbot setup:

1. Upload the widget in the private integration.
2. Add the widget to the Salesbot designer.
3. Set the widget URL to `https://<store-domain>/webhooks/kommo/salesbot`.
4. Verify these placeholders in the real Kommo account before production: `{{message_text}}`, `{{lead.id}}`, `{{contact.id}}`, `{{origin}}`, `{{lead.responsible.id}}`.
5. Configure success and fail exits in the Salesbot flow.

The widget contains no secrets and no production domain.
