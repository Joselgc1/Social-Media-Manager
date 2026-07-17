define(['jquery'], function ($) {
  return function SocialMediaManagerKommoWidget() {
    const self = this;

    function normalizeBackendUrl(value) {
      try {
        const parsed = new URL(String(value || '').trim());

        if (parsed.protocol !== 'https:') {
          return null;
        }

        if (parsed.username || parsed.password) {
          return null;
        }

        if (
          parsed.hostname === 'localhost' ||
          parsed.hostname === '127.0.0.1' ||
          parsed.hostname === '::1'
        ) {
          return null;
        }

        if (!parsed.pathname.endsWith('/webhooks/kommo/salesbot')) {
          return null;
        }

        return parsed.toString();
      } catch (_error) {
        return null;
      }
    }

    this.callbacks = {
      settings: function () {
        return true;
      },

      render: function () {
        return true;
      },

      init: function () {
        return true;
      },

      bind_actions: function () {
        return true;
      },

      onInstall: function () {
        return true;
      },

      onSave: function (widgetConfiguration) {
        const active = String(
          widgetConfiguration && widgetConfiguration.active
            ? widgetConfiguration.active
            : ''
        ).toLowerCase();

        if (active !== 'y') {
          return true;
        }

        const fields =
          widgetConfiguration && widgetConfiguration.fields
            ? widgetConfiguration.fields
            : {};

        const backendUrl = normalizeBackendUrl(fields.backend_url);

        if (!backendUrl) {
          self.set_status('error');
          return false;
        }

        self.set_settings({ backend_url: backendUrl });
        self.set_status('installed');
        return true;
      },

      destroy: function () {
        return true;
      },

      onSalesbotDesignerSave: function (_handlerCode, params) {
        const blockParams = params && params.params ? params.params : params;
        const webhookUrl = normalizeBackendUrl(
          blockParams && blockParams.webhook_url
        );

        if (!webhookUrl) {
          throw new Error(
            'Enter the HTTPS Social Media Manager Salesbot callback URL in this Salesbot block.'
          );
        }

        const requestData = {
          message: '{{message_text}}',
          lead_id: '{{lead.id}}',
          contact_id: '{{contact.id}}',
          origin: '{{origin}}'
        };

        const flow = [
          {
            question: [
              {
                handler: 'widget_request',
                params: {
                  url: webhookUrl,
                  data: requestData
                }
              },
              {
                handler: 'goto',
                params: {
                  type: 'question',
                  step: 1
                }
              }
            ],
            require: []
          },
          {
            question: [
              {
                handler: 'conditions',
                params: {
                  logic: 'and',
                  conditions: [
                    {
                      term1: '{{json.status}}',
                      term2: 'success',
                      operation: '='
                    }
                  ],
                  result: [
                    {
                      handler: 'exits',
                      params: {
                        value: 'success'
                      }
                    }
                  ]
                }
              },
              {
                handler: 'exits',
                params: {
                  value: 'fail'
                }
              }
            ],
            require: []
          }
        ];

        return JSON.stringify(flow);
      }
    };

    return this;
  };
});
