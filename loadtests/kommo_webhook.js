import http from 'k6/http';
import { check, sleep } from 'k6';

const VUS = Number(__ENV.VUS || 50);
const ITERATIONS = Number(__ENV.ITERATIONS || 3);

export const options = {
  scenarios: {
    customers: {
      executor: 'per-vu-iterations',

      // Number of simultaneous customers
      vus: VUS,

      // Number of messages sent by each customer
      iterations: ITERATIONS,

      maxDuration: '2m',
    },
  },

  thresholds: {
    // No more than 1% HTTP failures
    http_req_failed: ['rate<0.01'],

    // More than 99% of checks must succeed
    checks: ['rate>0.99'],

    // Kommo standard webhook acknowledgement requirement
    http_req_duration: ['p(95)<2000'],
  },
};

export default function () {
  const customer = __VU;
  const message = __ITER;

  // RUN_ID is generated once per k6 invocation.
  const runId = __ENV.RUN_ID || '1';

  // Keep all identity fields unique between test runs while preserving
  // one stable identity across the three messages from this customer.
  const customerKey = `${runId}-${customer}`;

  // Synthetic numeric IDs for fields that normally contain Kommo IDs.
  // RUN_ID is supplied with `date +%s`, so this remains numeric.
  const numericRunId = Number(runId);
  const contactId = String(numericRunId * 1000 + customer);
  const entityId = String(numericRunId * 1000 + 500 + customer);

  const payload = {
    add: [
      {
        id: `${runId}-load-message-${customer}-${message}`,

        chat_id: `load-chat-${customerKey}`,
        talk_id: `load-talk-${customerKey}`,

        contact_id: contactId,

        entity_id: entityId,
        entity_type: 'lead',

        text: `Load test message ${message} from customer ${customer}`,

        message_type: 'text',
        origin: 'whatsapp',

        author: {
          id: `load-author-${customerKey}`,
          name: `Load Customer ${customer}`,
          type: 'external',
        },
      },
    ],
  };

  // Avoid accidental //webhooks/... paths.
  const baseUrl = __ENV.BASE_URL.replace(/\/+$/, '');

  const url =
    `${baseUrl}/webhooks/kommo/events/${__ENV.KOMMO_WEBHOOK_SECRET}`;

  const response = http.post(
    url,
    JSON.stringify(payload),
    {
      headers: {
        'Content-Type': 'application/json',
      },
    }
  );

  check(response, {
    'webhook returned 200': (r) => r.status === 200,
  });

  // Simulate realistic pauses between consecutive customer messages.
  sleep(1 + Math.random() * 3);
}