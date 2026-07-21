# End-to-end incident workflow

```mermaid
flowchart TD
    A["IBM MQ / ACE<br/>Read-only MCP server"] --> B["Enterprise collector<br/>collect_forever()"]
    B --> C["Normalized observations"]
    C --> D["Deterministic rules<br/>threshold · DLQ · channel · trend"]
    D -->|Healthy| E["Dashboard: object status<br/>No incident · No AI"]
    D -->|Finding| F["Correlation & deduplication"]

    F -->|Duplicate| G["Suppress repeat alert<br/>No AI"]
    F -->|New incident| H["Search approved KB"]

    H -->|Strong approved match| I["Create incident<br/>KB reuse · No AI"]
    H -->|No strong match| J{"AI enabled and<br/>severity eligible?"}

    J -->|No| K["Create rule-based incident<br/>No AI"]
    J -->|Yes| L["Diagnostician<br/>AI investigation"]
    L -->|Success| M["Report Writer<br/>AI ticket-ready report"]
    M --> N["Create AI incident"]
    L -->|Failure or timeout| O["Create fallback incident<br/>AI failed · manual investigation"]

    I --> P["SQLite incident store"]
    K --> P
    N --> P
    O --> P

    P --> Q["Dashboard<br/>Incident list · details · timestamps"]
    Q --> R["Human review"]
    R --> S["Create KB draft"]
    S --> T["Edit + approve in KB tab"]
    T --> U["knowledge/approved/*.md"]
    U --> H
```

The collector and rule engine always run without AI. AI is conditional on a
new, eligible incident without a strong approved knowledge-base match. Any AI
failure still creates a visible fallback incident for manual investigation.
