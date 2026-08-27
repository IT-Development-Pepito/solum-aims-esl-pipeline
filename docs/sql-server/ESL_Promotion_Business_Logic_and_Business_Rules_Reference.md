# ESL Promotion Business Logic and Business Rules Reference

## 1. Purpose

This document defines the current business logic and business rules that should be used as a reference when replacing the existing `RefreshESL_New` stored procedure and related ESL synchronization process.

The objective is not to redesign the promotion process yet. The replacement system should first reproduce the current operational behavior in a controlled, testable, and maintainable way, while clearly isolating rules that are confirmed from rules that are still unresolved.

This document reflects the latest discussion with the related business departments.

---

## 2. Core Business Principle

The current promotion process is not governed by a fully standardized business process.

Promotion definitions can change during an active campaign because the responsible department may revise:

- promotion type;
- promotion category/group;
- promotion value;
- campaign description;
- campaign period;
- campaign terms;
- display wording.

Therefore, the replacement system must assume that promotion data can change at any time and must re-evaluate the current source data on every processing cycle.

The system must not assume that a campaign definition is immutable after it has been created.

---

## 3. Source-of-Truth Principle

The replacement system should distinguish between:

1. **Operational source data**
   Source tables that represent the campaign configuration used by store operations.

2. **Derived or warehouse metadata**
   Data such as `FactCampaign` that may be used as supporting information but should not automatically override the operational campaign data.

3. **ESL final state**
   The final article/promotion state that will be synchronized to SOLUM AIMS / ESL.

The replacement system should always preserve traceability between:

```text
source campaign
→ eligibility evaluation
→ UOM normalization
→ price calculation
→ selected promotion state
→ final ESL payload
```

Every final promotion state should be explainable from the source campaign data that produced it.

---

## 4. Campaign Eligibility

### 4.1 Date and Time Are the Primary Eligibility Rules

Campaign eligibility should be determined directly from:

- `PROMO_START_DATE`;
- `PROMO_END_DATE`;
- `PROMO_START_TIME`;
- `PROMO_END_TIME`.

The replacement system should evaluate whether the current processing timestamp falls inside the campaign date and time window.

Conceptually:

```text
current_date BETWEEN start_date AND end_date
AND
current_time is inside start_time → end_time
```

The implementation must also support campaign time windows that cross midnight.

Example:

```text
Start Time = 22:00
End Time   = 02:00
```

The campaign is valid from 22:00 until 02:00 the following day.

### 4.2 Do Not Depend on Campaign Status as the Main Filter

Campaign status should no longer be treated as the authoritative rule for determining whether a campaign is active.

The business decision is:

> Campaign validity should primarily follow the campaign start/end date and start/end time.

A campaign status field may still be retained for:

- diagnostics;
- monitoring;
- audit;
- comparison with the source system.

However, the replacement system should not reject an otherwise valid campaign only because a status field is inconsistent with its current date/time window, unless a future confirmed rule explicitly requires this behavior.

### 4.3 Re-evaluate Eligibility on Every Run

Because campaigns may be revised while already running, eligibility must be recalculated from the latest source data on every execution.

The replacement system should not rely on a previously selected promotion simply because it was valid during the previous cycle.

---

## 5. Regular Selling Price

The ESL regular selling price for a physical store must use:

```text
BSP_PRICE_CATG = '001'
```

Category `001` is the authoritative physical-store regular selling price.

Other categories must not be used as the ESL base price unless a future business rule explicitly defines otherwise.

For example:

```text
001 = physical-store selling price
007 = application / other sales-channel price
```

The replacement system must not select another price category as a fallback merely because it exists.

If category `001` is missing or ambiguous, the condition should be logged as a data-quality exception.

---

## 6. Promotion Types

The current process recognizes at least the following promotion types:

```text
PERCENT BASED
FIXED PRICE
VALUE BASED
```

Each type must be represented explicitly in the domain model.

The replacement system should not infer the promotion type only from free-form text.

### 6.1 Percent-Based Promotion

For a percent-based promotion:

```text
promo_percent > 0
```

The effective price can be calculated as:

```text
effective_price =
regular_price - (regular_price × promo_percent / 100)
```

Example:

```text
Regular Price : 74,500
Discount      : 50%

Effective Price:
74,500 - (74,500 × 50%)
= 37,250
```

A percent-based promotion with:

```text
promo_percent <= 0
```

must not be treated as a valid structured promotion.

### 6.2 Fixed-Price Promotion

For a fixed-price promotion:

```text
promo_price > 0
```

The current safety rule is:

```text
promo_price > regular_price
→ treat as no promotion
```

The comparison is strictly `>`.

A fixed promotion equal to the regular price is not rejected by this specific rule unless another business rule later defines it as invalid.

### 6.3 Value-Based Promotion

Value-based promotions may contain terms such as:

```text
SAVE 60,000 PER CTN
```

The current process does not yet have a complete generic rule for converting every value-based promotion into a selling-UOM price/display state.

These cases must be handled explicitly and must not be approximated.

---

## 7. Promotion Priority

### 7.1 Current Business Decision

Promotion priority is still not formally defined.

The replacement system must therefore **not invent a new promotion-priority algorithm**.

Do not automatically implement rules such as:

```text
lowest price always wins
fixed price always wins
percent promotion always wins
newest campaign always wins
CLEARANCE always wins
IN STORE PROMO always wins
```

unless the business formally approves such a rule.

### 7.2 Use Current Behavior as the Initial Reference

Until a formal promotion-priority rule exists, the replacement system should use the current production behavior as the compatibility baseline.

The selection logic should be deterministic and reproducible, but it must not introduce a new business interpretation.

This means:

- preserve the current effective selection behavior where possible;
- log ambiguous candidates;
- retain enough information to explain why a candidate was selected;
- make future priority rules configurable instead of hard-coded into infrastructure.

### 7.3 Different Economic Outcomes

A business ambiguity exists when multiple currently eligible campaigns for the same Item/UOM produce different economic outcomes.

Example:

```text
Regular Price = 74,500

Campaign A:
50% discount
Effective Price = 37,250

Campaign B:
Fixed Price = 57,900
```

Both campaigns may be valid according to date/time rules, but the business has not yet defined which one should win.

The replacement system should detect and record this as:

```text
PROMO_PRIORITY_DIFFERENT_ECONOMIC
```

Recommended diagnostic information:

```text
store
item
UOM
campaign code
promotion type
regular price
promotion price
promotion percent
calculated effective price
campaign date/time
campaign terms
```

This condition must remain observable even if the compatibility logic chooses one candidate.

---

## 8. Same Economic Outcome but Different Campaign Terms

Multiple eligible campaigns may produce the same economic result while containing different campaign descriptions or business terms.

Example:

```text
Campaign A → 50% discount, LIMIT 2
Campaign B → 50% discount, LIMIT 5
Campaign C → 50% discount, LIMIT 12
```

The final price is identical, but the campaign terms are not necessarily equivalent.

The replacement system should detect this condition as:

```text
DISPLAY_PRIORITY_SAME_ECONOMIC
```

The term "display difference" should not be interpreted as purely cosmetic.

Fields such as:

```text
LIMIT
campaign description
campaign group
promotion period
campaign wording
```

may represent actual promotion terms.

Until a formal rule exists, the system should preserve current behavior and expose the ambiguity for monitoring or review.

---

## 9. `DISC_TEXT`

### 9.1 `DISC_TEXT` Is Manual Input

`DISC_TEXT` is manually entered by users from the related department.

Therefore, the replacement system must assume that human errors may occur at any time.

Possible anomalies include:

```text
inconsistent formatting
different separators
typing mistakes
different wording for the same campaign
different limit notation
missing fields
additional fields
incorrect or outdated description
```

### 9.2 Do Not Use `DISC_TEXT` as the Primary Business Logic Source

Structured promotion logic must come from structured source fields whenever available.

`DISC_TEXT` should mainly be treated as:

- display text;
- descriptive information;
- an audit/reference field.

The replacement system should not determine:

```text
promotion type
promotion value
priority
campaign eligibility
price calculation
```

solely by parsing `DISC_TEXT`.

### 9.3 Do Not Enforce a Fixed Text Structure

Because the business process is not standardized and text is manually entered, the replacement system must not assume a fixed number of `|`-separated fields.

Examples such as:

```text
3 fields
4 fields
5 fields
```

must not automatically be rejected only because their field counts differ.

### 9.4 Preserve Raw Source Text

The original campaign description should be retained for:

- audit;
- troubleshooting;
- downstream display;
- comparison with the source.

If normalization is required, store both:

```text
raw_disc_text
normalized_disc_text
```

rather than destroying the original value.

---

## 10. PFS / Member Promotion

PFS/member promotions must not be displayed on ESL.

The current explicit exclusion rule is:

```text
campaign description/group contains PFS
→ exclude from ESL promotion
```

The system should not implement a generic:

```text
contains MEMBER
```

filter unless the business formally approves it.

The PFS exclusion should be explicit and testable.

---

## 11. UOM Handling

### 11.1 Actual Selling UOM

Promotion calculation should be performed using the item's actual selling UOM.

The general processing sequence should be:

```text
campaign
→ determine actual selling UOM
→ obtain category-001 regular price for that UOM
→ calculate promotion
→ transform to ESL display UOM
```

### 11.2 CLR

`CLR` should not be interpreted as a literal selling UOM.

Confirmed behavior:

```text
Campaign UOM = CLR
→ map to item's actual selling UOM
```

This normalization is allowed.

### 11.3 Non-CLR UOM Mismatch

If:

```text
campaign UOM != actual selling UOM
AND
campaign UOM != CLR
```

the system must not invent a conversion.

Example:

```text
Campaign UOM = CTN
Actual UOM   = PCS
```

This condition should be recorded as:

```text
NON_CLR_UOM_CONVERSION
or
UOM_RULE_REQUIRED
```

The campaign should only be converted when an authoritative conversion rule/master is available.

### 11.4 Known Example

```text
Item         : 101011000333
Product      : BINTANG PINT 330ML
Campaign UOM : CTN
Selling UOM  : PCS
Terms        : SAVE 60,000 PER CTN
```

The replacement system must not assume the number of PCS per CTN.

---

## 12. Scalable Items

For scalable items sold as kilograms but displayed on ESL per 100 grams:

```text
selling UOM = KGS
display UOM = /100GR
```

Price transformation:

```text
per_100gr_regular_price = regular_price / 10
per_100gr_promo_price   = effective_promo_price / 10
```

Promotion eligibility and calculation must happen **before** the display-UOM transformation.

The system must not evaluate the campaign directly against `/100GR` as if it were the source selling UOM.

---

## 13. Weekday Metadata

The current process may use `FactCampaign` as supporting weekday metadata.

However, cases exist where:

```text
operational campaign exists
date/time is active
FactCampaign weekday metadata is missing
```

The current compatibility behavior is:

```text
campaign remains eligible
```

This is effectively an "eligible by fallback" rule.

The replacement system should preserve this behavior until the business decides otherwise.

It should also record the condition as:

```text
MISSING_WEEKDAY_METADATA
```

so it can be monitored.

The system must clearly distinguish:

```text
metadata missing
```

from:

```text
metadata exists and explicitly says the campaign is inactive today
```

---

## 14. Campaign Data Can Change Mid-Campaign

The responsible department may revise campaign definitions during an active period.

Therefore, the replacement system must support:

```text
promotion type changes
promotion category/group changes
promotion value changes
description changes
date/time changes
campaign-term changes
```

without requiring code changes.

Business configuration must remain data-driven.

The system should compare the latest source campaign against the previous synchronized state and generate a new ESL state when a material change occurs.

---

## 15. Human Error and Data Quality

Because some campaign fields are manually maintained, the replacement system must expect imperfect source data.

The correct response to imperfect data is not to silently fabricate a business rule.

The system should classify source anomalies into categories such as:

```text
INVALID_PROMOTION_VALUE
FIXED_PRICE_GT_REGULAR
ZERO_EFFECT_PROMOTION
NON_CLR_UOM_CONVERSION
MISSING_WEEKDAY_METADATA
PROMO_PRIORITY_DIFFERENT_ECONOMIC
DISPLAY_PRIORITY_SAME_ECONOMIC
AMBIGUOUS_REGULAR_PRICE
MALFORMED_DISPLAY_TEXT
```

Each anomaly should contain enough source information for investigation.

---

## 16. Final Promotion State Must Be Atomic

All promotion fields written to the ESL state must come from one selected promotion candidate.

The system must never create a mixed state where:

```text
DISC_TEXT comes from Campaign A
PROMO_PERCENT comes from Campaign B
PROMO_PRICE comes from Campaign C
CAMPAIGN_GROUP comes from Campaign D
```

A final promotion state should be represented as one domain object, for example:

```text
PromotionState
- campaign_code
- promotion_type
- campaign_group
- regular_price
- promo_price
- promo_percent
- effective_price
- disc_text
- start_date
- end_date
- start_time
- end_time
- source_uom
- resolved_uom
```

The state should be persisted or synchronized atomically.

---

## 17. Store Isolation

Every store must be processed independently.

Data from one store must never be reused when processing another store.

The replacement system should enforce store context explicitly for:

```text
item master
regular price
stock
campaign
promotion selection
ESL state
logging
```

A processing unit should conceptually be:

```text
Store
  → Item
    → UOM
      → Eligible Campaign Candidates
        → Selected Promotion State
```

Store context must be part of every business key and query boundary.

---

## 18. Recommended Business Key

The logical evaluation key should be:

```text
STORE_CODE + ITEM_CODE + SELLING_UOM
```

The ESL final persistence model may have a different physical key if required by SOLUM/AIMS, but promotion evaluation should not merge candidates across stores or unrelated UOMs.

---

## 19. Recommended Processing Flow for the Replacement System

```text
1. Load store configuration
2. Load current active/open item master
3. Resolve actual selling UOM
4. Load category-001 regular price
5. Load current campaign source data
6. Evaluate campaign date/time eligibility
7. Apply explicit exclusions such as PFS
8. Enrich optional weekday metadata
9. Normalize CLR → actual selling UOM
10. Detect unsupported non-CLR UOM conversion
11. Validate promotion values
12. Calculate economic outcome
13. Group candidates by Store + Item + UOM
14. Detect promotion ambiguity
15. Apply current compatibility selection behavior
16. Build one atomic PromotionState
17. Apply scalable-item display transformation
18. Compare with previous ESL state
19. Produce changed/new/removed state
20. Synchronize downstream
21. Persist audit and anomaly logs
```

---

## 20. Rules That Are Confirmed

The following rules can be treated as confirmed requirements for the replacement system:

| Rule | Current Decision |
|---|---|
| Regular physical-store price | Use price category `001` |
| Campaign activity | Primarily evaluate start/end date and time |
| Campaign status | Do not use as the primary eligibility authority |
| Campaign definitions | Can change during an active campaign |
| `DISC_TEXT` | Manual input; may contain human error |
| `DISC_TEXT` structure | Do not require a fixed field count |
| PFS | Must not be displayed on ESL |
| Generic `MEMBER` filtering | Do not apply unless explicitly approved |
| CLR UOM | Normalize to actual selling UOM |
| Non-CLR UOM mismatch | Do not invent conversion |
| Fixed price > regular price | Treat as no promotion |
| Percent <= 0 | Invalid structured percent promotion |
| Fixed price <= 0 | Invalid structured fixed promotion |
| Scalable KGS display | Convert to `/100GR` after calculation |
| Store processing | Must be isolated per store |
| Promotion state | Must be atomic and come from one candidate |

---

## 21. Rules That Are Still Unresolved

The following rules must remain explicitly marked as unresolved:

### 21.1 Promotion Priority

No formal business priority exists when multiple eligible campaigns produce different economic outcomes.

Initial replacement behavior:

```text
preserve current production behavior
+
log ambiguity
+
do not introduce new priority semantics
```

### 21.2 Same-Economic Campaign Terms Priority

No formal rule exists for selecting which campaign text/terms should be displayed when multiple campaigns produce the same price or discount.

### 21.3 Non-CLR UOM Conversion

No authoritative generic conversion rule has been confirmed for cases such as:

```text
CTN → PCS
```

### 21.4 Missing Weekday Metadata

No final business decision has been made on whether warehouse weekday metadata is mandatory.

Initial replacement behavior should preserve the current fallback:

```text
operational campaign valid by date/time
+
weekday metadata missing
→ remain eligible
```

until this rule is formally changed.

---

## 22. Compatibility Requirement for the Replacement System

The first version of the replacement application should prioritize **behavioral compatibility** over business redesign.

The system should:

```text
reproduce confirmed current rules
preserve current unresolved selection behavior
make ambiguity observable
separate business rules from infrastructure
avoid hidden SQL-side business logic
make future rules configurable and testable
```

A business-rule change should be implementable without changing unrelated:

```text
database access
scheduler
API integration
file generation
logging
deployment infrastructure
```

This separation is one of the primary goals of replacing the existing stored-procedure and ETL-based implementation.

---

## 23. Recommended Rule Architecture

The replacement system should separate the following rule components:

```text
CampaignEligibilityRule
RegularPriceRule
PromotionValueValidationRule
PromotionPriorityRule
CampaignDisplayRule
UOMResolutionRule
ScalableItemRule
MemberPromotionExclusionRule
WeekdayMetadataRule
PromotionStateBuilder
```

Each rule should be independently testable.

Example:

```text
PromotionPriorityRule
```

should initially implement:

```text
CurrentCompatibilityStrategy
```

and later be replaceable by a formally approved strategy without changing the rest of the pipeline.

Possible future strategies may include:

```text
POSPriorityStrategy
ConfiguredPriorityMatrixStrategy
```

but they must not be implemented until approved.

---

## 24. Auditability Requirement

For every generated ESL promotion state, the replacement system should be able to answer:

```text
Which campaign was selected?
Which other campaigns were eligible?
What regular price was used?
Which UOM was used?
Was UOM normalized?
What promotion calculation was performed?
Why was another campaign not selected?
Was weekday metadata available?
Was fallback behavior used?
What source values generated DISC_TEXT?
When was the decision made?
```

This auditability is required because the business process allows campaign revisions and manual input.

---

## 25. Summary

The replacement system should not attempt to "fix" the business process by inventing rules.

The implementation baseline is:

```text
Use date/time as the primary campaign eligibility rule.
Treat campaign definitions as mutable.
Treat DISC_TEXT as untrusted manual display input.
Use category 001 as the physical-store regular price.
Explicitly exclude PFS.
Normalize CLR to actual selling UOM.
Do not invent non-CLR UOM conversions.
Build promotion state atomically.
Keep stores isolated.
Preserve current promotion-priority behavior until a formal rule exists.
Log every unresolved ambiguity.
```

The most important architectural requirement is to move these rules out of tightly coupled SQL/ETL implementation and represent them as explicit, independently testable business rules in the replacement application.
