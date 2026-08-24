# Impact analysis evaluation

- Rewire version: `0.1.0`
- Minimum confidence: `0.35`
- Dataset: `evals/datasets/impact`
- Duration: 0.03s

## Overall

| Granularity | Precision | Recall | F1 | TP | FP | FN |
| --- | --- | --- | --- | --- | --- | --- |
| location | 1.000 | 1.000 | 1.000 | 9 | 0 | 0 |
| file | 1.000 | 1.000 | 1.000 | 6 | 0 | 0 |

## Cases

### `decoys`

One genuine SDK call site surrounded by decoys: a local helper with an identically named parameter, an unrelated dict key, the name inside a log string, and a different library that also accepts max_tokens. Measures precision, which recall-only datasets cannot.

**request_field_removed:max_tokens** — P=1.000 R=1.000 F1=1.000 (tp=1 fp=0 fn=0)

### `openai_max_tokens`

OpenAI's max_tokens -> max_completion_tokens rename against an application that reaches the SDK three ways: a module-level client, an instance attribute, and a dict payload forwarded with **kwargs.

**request_field_removed:max_tokens** — P=1.000 R=1.000 F1=1.000 (tp=5 fp=0 fn=0)

### `raw_http`

A repository calling the API over raw HTTP with no SDK installed. Name resolution finds nothing, so the endpoint path is the only available handle. Measures whether the path strategy earns its place.

**request_field_removed:max_tokens** — P=1.000 R=1.000 F1=1.000 (tp=1 fp=0 fn=0)

### `response_field`

A response field becoming optional, in a repository that both reads it and constructs it. Reading it is what breaks; the test doubles that write it are unaffected. Separating the two requires knowing which direction the field travels, which name matching alone cannot.

**response_field_became_optional:usage.completion_tokens** — P=1.000 R=1.000 F1=1.000 (tp=2 fp=0 fn=0)

### `unrelated`

A repository that never touches the API but reuses its vocabulary: model, max_tokens, completion_tokens, total_tokens. Every target expects zero locations. Reporting anything here is a false positive, and a benchmark without such a case cannot tell a careful analyser from an eager one.

**request_field_removed:max_tokens** — P=1.000 R=1.000 F1=1.000 (tp=0 fp=0 fn=0)

**response_field_became_optional:usage.completion_tokens** — P=1.000 R=1.000 F1=1.000 (tp=0 fp=0 fn=0)
