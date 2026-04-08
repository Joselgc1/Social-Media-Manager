# Test Chat Command

Send a test message to the store chatbot running locally. The user's message is: $ARGUMENTS

If no arguments provided, use "Hola, tienen pijamas?" as the default message.

Run:

```bash
curl -s -X POST http://localhost:8000/test/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "<the message>"}'
```

Parse the JSON response and show me:

- The AI reply text
- Whether any tool calls were made
- Whether any interactive buttons were returned
- The provider and model used
- Token usage if available
