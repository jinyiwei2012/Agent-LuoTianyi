# Minecraft v1 semantic fixtures

`schema.json` is the Draft 2020-12 structural contract for the eight frozen
Minecraft v1 envelope types. It is intentionally a new protocol contract; it
does not reinterpret the historical `contracts/examples` Map examples.

The schema enforces exact envelope/payload keys, discriminators, primitive
bounds, fixed M1 observation source/audience, and the constructible enum/null
shapes. Fixtures and consumers must additionally enforce the frozen semantic
rules: duplicate JSON keys, UTF-8/BOM/surrogate rejection, JSON numeric lexical
rules (`I63` and finite `F64`), exact Gregorian UTC validation, byte/scalar and
frame/depth limits, trusted binding/authentication, timestamp/deadline order,
request-record correlation, stamp monotonicity, receipt/outcome and reconcile
matrices, intent-to-success-kind correlation, response/request self-reference, evidence
requirements, and runtime state/capacity checks. JSON Schema
cannot establish those facts from one parsed JSON value.

`manifest.json` records the frozen specification SHA-256 and per-fixture byte
hash. Every listed fixture, including raw lexical fixtures, has a byte hash.
`expectedLayer: "schema"` is evaluated after JSON parsing by `schema.json`;
`expectedLayer: "semantic"` deliberately parses and passes structural validation but
must be rejected by correlation/lexical semantic validation. Raw invalid UTF-8 is
represented by a `.hex` source file; a harness must hex-decode it before UTF-8 parsing.
