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

    function unwrapSettingValue(value) {
      if (typeof value === 'string') {
        return value;
      }

      if (value && typeof value === 'object') {
        return (
          value.value_manual ||
          value.value ||
          value.url ||
          value.text ||
          ''
        );
      }

      return '';
    }

    function getBlockParams(params) {
      if (!params || typeof params !== 'object') {
        return {};
      }

      if (params.params && typeof params.params === 'object') {
        return params.params;
      }

      return params;
    }

    function getInstalledBackendUrl() {
      if (!self.get_settings) {
        return null;
      }

      const settings = self.get_settings() || {};
      const candidates = [
        settings.backend_url,
        settings.fields && settings.fields.backend_url,
        settings.params && settings.params.backend_url
      ];

      for (const candidate of candidates) {
        const normalized = normalizeBackendUrl(
          unwrapSettingValue(candidate)
        );

        if (normalized) {
          return normalized;
        }
      }

      return null;
    }

    function interactionTypeForHandler(handlerCode) {
      if (handlerCode === 'kommo_ai_instagram_comment') {
        return 'instagram_comment';
      }

      return 'private_message';
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

        return Boolean(normalizeBackendUrl(fields.backend_url));
      },

      destroy: function () {
        return true;
      },

      salesbotDesignerSettings: function (_body, _renderRow, _params) {
        return {
          exits: [
            {
              code: 'success',
              title: self.i18n('salesbot').success_exit
            },
            {
              code: 'fail',
              title: self.i18n('salesbot').fail_exit
            }
          ]
        };
      },

      onSalesbotDesignerSave: function (handlerCode, params) {
        const blockParams = getBlockParams(params);
        const blockUrl = normalizeBackendUrl(
          unwrapSettingValue(blockParams.webhook_url)
        );
        const webhookUrl = blockUrl || getInstalledBackendUrl();
        const interactionType = interactionTypeForHandler(handlerCode);

        if (!webhookUrl) {
          console.warn('Kommo Salesbot widget configuration is invalid', {
            handlerCode: handlerCode,
            parameterKeys: Object.keys(blockParams)
          });
          throw new Error(
            'Configure the Salesbot callback URL in the integration settings or in this widget block.'
          );
        }

        const requestData = {
          message: '{{message_text}}',
          lead_id: '{{lead.id}}',
          contact_id: '{{contact.id}}',
          origin: '{{origin}}',
          interaction_type: interactionType
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

        const serializedFlow = JSON.stringify(flow);
        JSON.parse(serializedFlow);
        return serializedFlow;
      }
    };

    return this;
  };
});
