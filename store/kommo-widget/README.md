# Kommo Private Salesbot Widget

This widget must be installed at the Kommo account level before it appears as an installed widget inside Salesbot.

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
10. Refresh Kommo.
11. Open Salesbot.
12. Add a Widget step.
13. Select Social Media Manager AI from the installed widget list.

The widget uses `installation=true`, has both `settings` and `salesbot_designer` locations, and requires the top-level `backend_url` setting. The Salesbot block can override the callback URL with its own `webhook_url`, but if that field is empty it uses the installed widget `backend_url`.

The Salesbot source uses `widget_request` followed by `goto` question step `1`. The backend resumes the flow by calling Kommo's continuation URL.

If invalid manifests were previously uploaded and Kommo continues using stale metadata, create a fresh private integration or regenerate the Widget code/key before uploading the corrected archive.

Production notes:

- Keep the callback URL on the store domain, not the master domain.
- The widget contains no secrets and no production domain.
- Test the uploaded widget in a real Kommo account before relying on production delivery because placeholder availability can vary by Kommo account/channel.
