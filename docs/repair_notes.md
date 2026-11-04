# Repair notes — selected-account business problem validation

## Symptom

Ollama generation could fail with:

`business_problem must refer to the selected account`

The model could accurately describe the business problem from the brief while omitting the account name. The previous validator treated this as a model-generation error and spent another Ollama call attempting to repair it.

## Change

Added `_ensure_business_problem_account_reference` in `agent.py`. Before validation, it prefixes the selected account name to the existing business-problem statement only when the exact account name is absent. The selected account is sourced from the opportunity context, not inferred by the model. The model's substantive problem description is preserved, and the existing validator still runs.

The normalization is applied after initial plan parsing and after a model validation repair, so both paths behave consistently.

## Verification

Added tests for a missing account reference and for avoiding duplicate account names. Ran:

`python -m unittest discover -s tests -v`

Result: 44 tests passed.
