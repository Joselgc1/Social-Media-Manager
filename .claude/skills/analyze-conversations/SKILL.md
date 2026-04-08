---
name: analyze-conversations
description: Analyze recent chatbot conversations for quality and improvement opportunities
disable-model-invocation: true
context: fork
---

# Analyze Conversations

Analyze recent chatbot conversations from the store database to identify quality issues and improvement opportunities.

## Steps

1. Read the `DATABASE_URL` from `store/.env`
2. Connect to the database and pull recent conversations (last 7 days or last 100 conversations, whichever is smaller)
3. For each conversation thread (grouped by customer_id), analyze:

### Metrics to compute

- **Response quality:** Did the AI answer the customer's question? Were there hallucinations or off-topic responses?
- **Escalation rate:** How many conversations were escalated to human? What triggered escalation?
- **Tool usage patterns:** Which tools get called most? Are any tools never used?
- **Conversion rate:** What percentage of conversations led to an order being created?
- **Unanswered questions:** What questions did customers ask that the AI couldn't handle well?
- **Language quality:** Are responses in proper Spanish? Any English leaking through?
- **Average conversation length:** How many messages before resolution?

### Queries to use

```sql
-- Recent conversations with customer info
SELECT c.display_name, c.channel, conv.role, conv.content, conv.created_at
FROM conversations conv
JOIN customers c ON c.id = conv.customer_id
WHERE conv.created_at > NOW() - INTERVAL '7 days'
ORDER BY c.id, conv.created_at;

-- Escalation count
SELECT COUNT(DISTINCT customer_id) FROM customers WHERE conversation_state = 'escalated';

-- Orders in the period
SELECT COUNT(*) FROM orders WHERE created_at > NOW() - INTERVAL '7 days';

-- Usage log for tool calls
SELECT * FROM usage_log WHERE created_at > NOW() - INTERVAL '7 days' ORDER BY created_at DESC;
```

4. Present findings as a structured report with:
   - Executive summary (2-3 sentences)
   - Key metrics table
   - Top 5 issues found (with example conversation excerpts)
   - Recommended improvements (prioritized by impact)
