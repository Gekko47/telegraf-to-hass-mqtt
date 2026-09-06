# QA/QC Implementation Plan

## Phase 1: Critical Fixes (Do First)
1. Add pre-commit config - .pre-commit-config.yaml with ruff, mypy, pytest
2. Fix coverage gap - Add tests for _apply_tag_unit_mapping edge cases
3. Fix binary sensor coercion - Add false, 0, no, off to false set

## Phase 2: High Severity Fixes
4. Fix device ID collision - Use more topic segments or hash for host_topic strategy
5. Ensure frozen tags everywhere - Audit all parsers use frozen_tags()
6. Add parser error logging - Change DEBUG to WARNING for handler errors

## Phase 3: Medium Severity Fixes
7. Add missing icon mappings - system, kernel, processes, swap
8. Remove duplicate translation keys - Clean up naming.py
9. Add MQTT topic validation - In config_flow.py
10. Fix repairs deduplication - Track issued issues per cycle
11. Update ipmi_sensor.py docstring - Reflect new architecture

## Phase 4: Code Quality
12. Use timedelta for time constants - const.py
13. Fix type hints - Remove unnecessary | None or document purpose
14. Add validation warnings - For category overrides

## Phase 5: Testing & CI
15. Add integration tests - Full pipeline for new bug fixes
16. Improve Windows test stubs - More comprehensive POSIX shims
17. Document coverage pragmas - For the 7 uncovered lines

## Verification Checklist for Each Commit
- prek run --all-files (ruff linting + formatting)
- mypy --strict custom_components (type checking)
- pytest -q with 100% coverage
- hassfest validation
- hacs validation
