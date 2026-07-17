define(['jquery'], function ($) {
  return function SocialMediaManagerKommoWidget() {
    const createStep = function (handlers) {
      return { question: handlers, require: [] };
    };

    this.callbacks = {
      settings: function () { return true; },
      init: function () { return true; },
      bind_actions: function () { return true; },
      render: function () { return true; },
      destroy: function () { return true; },
      onSave: function () { return true; },

      onSalesbotDesignerSave: function (_handlerCode, params) {
        const webhookUrl = params && params.webhook_url ? params.webhook_url : '';
        const requestData = {
          message: '{{message_text}}',
          lead_id: '{{lead.id}}',
          contact_id: '{{contact.id}}',
          origin: '{{origin}}',
          responsible_user_id: '{{lead.responsible.id}}'
        };

        return JSON.stringify([
          createStep([
            {
              handler: 'widget_request',
              params: {
                url: webhookUrl,
                data: requestData
              }
            }
          ]),
          createStep([
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
                    params: { value: 'success' }
                  }
                ]
              }
            },
            {
              handler: 'exits',
              params: { value: 'fail' }
            }
          ])
        ]);
      }
    };

    return this;
  };
});
