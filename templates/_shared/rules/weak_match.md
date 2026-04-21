### Weak-match handling

`brain_recall` returns an envelope:

```json
{ "query": "...", "weak_match": bool, "top_score": float,
  "threshold": float, "guidance": "...", "hits": [ ... ] }
```

When `weak_match: true`, the top RRF score is below threshold — the
hits are "might-be-related-by-topic" matches, not "answers". Default
response: *"the brain has no record of this"*. Only mention a hit at
all if its snippet shares real tokens with the query; and even then,
stop at naming the file — do not paraphrase it into an answer. Never
bridge from "the note says X" to "…so the answer about Y is probably
Z". The weak-match flag exists precisely because such bridges were
being built on false foundations.

> Failure mode #1 (incident 2026-04-21, second occurrence):
> {{USERNAME}} deleted the "dép in bedroom" line from
> `Thuha va Trinh.md`, leaving only *"gio ho ve long xuyen roi"*.
> Asked *"đôi dép tôi đâu?"* the agent called `brain_recall`, got
> the note back as a weak semantic near-miss (RRF=0.026), and
> confidently answered *"Trong phòng ngủ — theo note Thuha va
> Trinh.md"* — a fabricated location with a fabricated citation.
> The snippet did not contain "phòng ngủ" at any point. Never do
> this.
>
> Failure mode #2 (same day, after `weak_match` shipped): with the
> flag working correctly, the agent fetched the full note, saw only
> *"gio ho ve long xuyen roi"*, then still answered *"dép cũng theo
> họ về Long Xuyên rồi"* — TWO errors stacked:
>   1. **Subject conflation**: question was *"đôi dép **tôi** đâu"*
>      ({{USERNAME}}'s dép). Note is about **Thuha/Trinh** going
>      somewhere. Possessive pronoun "tôi" makes the owner
>      unambiguous — {{USERNAME}}, not Thuha/Trinh. The hit fails
>      subject check and should have been rejected before any
>      location-reasoning started.
>   2. **Inferential chain**: even granting the (wrong) conflation,
>      the note says nothing about dép moving with them.
> The correct answer: *"note nhắc đến Thuha/Trinh về Long Xuyên
> nhưng đó không phải dép của bạn — brain không có thông tin về
> dép của {{USERNAME}}."* Always check the subject first; only then
> consider whether the hit even belongs to the question.
