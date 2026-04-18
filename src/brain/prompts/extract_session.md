# Session Extraction Prompt

You are a brain extraction agent. Analyze this conversation summary and extract structured knowledge.

## Existing entities in the brain
{existing_entities}

## Conversation summary
{conversation_summary}

## Instructions

Extract ALL of the following from the conversation. Be thorough — anything mentioned is worth capturing. Output ONLY valid JSON.

```json
{
  "people": [
    {
      "name": "Full Name",
      "role": "their role if mentioned",
      "company": "their company if mentioned",
      "facts": ["fact 1 about them", "fact 2"],
      "is_new": true
    }
  ],
  "clients": [
    {
      "name": "Company Name",
      "facts": ["fact 1", "fact 2"],
      "is_new": true
    }
  ],
  "projects": [
    {
      "name": "Project Name",
      "client": "Client if known",
      "facts": ["fact 1", "fact 2"],
      "is_new": true
    }
  ],
  "domains": [
    {
      "name": "Concept or Domain",
      "source_context": "work|study|conversation",
      "facts": ["what was learned"],
      "is_new": true
    }
  ],
  "decisions": [
    {
      "title": "What was decided",
      "context": "Why and by whom",
      "alternatives": ["what was considered but rejected"],
      "date": "YYYY-MM-DD"
    }
  ],
  "issues": [
    {
      "title": "Problem or complaint",
      "raised_by": "Person name",
      "about": "Client or project",
      "status": "open|resolved"
    }
  ],
  "insights": [
    {
      "content": "What was learned or realized",
      "source": "What triggered this insight",
      "confidence": "high|medium|low"
    }
  ],
  "corrections": [
    {
      "pattern": "What Claude did wrong",
      "correction": "What the user said instead",
      "rule": "General rule to follow in future"
    }
  ],
  "evolutions": [
    {
      "topic": "What the thinking is about",
      "old_position": "What the user used to think",
      "new_position": "What the user thinks now",
      "cause": "What changed their mind"
    }
  ],
  "contested": [
    {
      "topic": "What the conflict is about",
      "position_a": "First position",
      "position_b": "Second position",
      "source_a": "Where position A came from",
      "source_b": "Where position B came from"
    }
  ],
  "high_value_outputs": [
    {
      "title": "What the analysis/comparison/synthesis is about",
      "content": "The key output worth saving",
      "related_entities": ["entity names this connects to"]
    }
  ]
}
```

Rules:
- Set is_new to false if the entity already exists in the brain (check the existing entities list)
- For existing entities, only include NEW facts not already captured
- If nothing was learned about a category, use an empty array
- Capture corrections the user made to Claude — these are high priority
- Capture high-value outputs (analyses, comparisons, syntheses) that shouldn't vanish with the session
- Use exact names as they appear in conversation
- Dates in YYYY-MM-DD format
