# Kommo Private Salesbot Widget

This widget must be installed at the Kommo account level before it appears as an installed widget inside Salesbot.

The authoritative widget version is `widget.version` in [`manifest.json`](manifest.json) (currently `1.2.14`). Increment it every time a new archive is uploaded so Kommo refreshes the widget files.

Build:

```bash
cd store/kommo-widget
python3 build_widget.py --widget-code YOUR_WIDGET_CODE
```

Use the real Widget code from the private Kommo integration. The source manifest keeps `__WIDGET_CODE__`; the builder substitutes the real code only inside the ZIP manifest, validates the installable manifest, i18n files, PNG dimensions, and obvious secret markers, then overwrites `social-media-manager-kommo-widget.zip`.

Installation sequence:

1. Create or open the private Kommo integration.
2. Obtain the Widget code.
3. Build with `python3 build_widget.py --widget-code YOUR_WIDGET_CODE`.
4. Upload `social-media-manager-kommo-widget.zip` to the private integration.
5. Save the integration.
6. Return to Settings -> Integrations.
7. Open the Social Media Manager widget.
8. Enter `https://YOUR-STORE-DOMAIN/webhooks/kommo/salesbot` in `backend_url`.
9. Enable/install it and save the settings.
10. Disable and re-enable the integration, or refresh Kommo after the upload.
11. Hard refresh the browser if Salesbot still shows stale widget fields.
12. Open Salesbot.
13. Add a Widget step.
14. Select `Ask Eva AI for WhatsApp`.
15. Leave the Salesbot block's `Salesbot callback URL override` empty unless this block must call a different store backend.

The widget uses `installation=true`, has both `settings` and `salesbot_designer` locations, and requires the top-level `backend_url` setting. The Salesbot block `webhook_url` is an optional per-block override. If it is empty, the generated Salesbot source uses the installed account-level `backend_url`; if it is valid, it overrides the global URL.

The Salesbot source uses `widget_request` followed by `goto` question step `1`. The backend resumes the flow by calling Kommo's continuation URL. The widget exposes three Salesbot exits: `success` for text delivered by Salesbot, `media` for media already delivered by the backend Chats API, and `fail` for errors.

The widget is used only by the WhatsApp Kommo Salesbot. Route `success` to a WhatsApp-restricted Kommo Message step using `{{json.message}}`; route `media` directly to completion because the backend already delivered that attachment. Configure the Salesbot as `KOMMO_WHATSAPP_SALESBOT_ID`. Instagram is Meta-native and must not use this widget or any Kommo Salesbot.

If invalid manifests were previously uploaded first and Kommo continues using stale metadata, create a fresh private integration or regenerate the Widget code/key before uploading the corrected archive, following Kommo's widget update behavior.

Production notes:

- Keep the callback URL on the store domain, not the master domain.
- The widget contains no secrets and no production domain.
- Test the uploaded widget in a real Kommo account before relying on production delivery because placeholder availability can vary by Kommo account/channel.
