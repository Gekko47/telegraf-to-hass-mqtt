# Changelog

All notable changes to this project will be documented in this file.

## [1.1.0] - 2026-08-01

### Fixed
- Corrected the Phase 3 naming path so host metadata no longer leaks into the user-visible entity title.
- Normalized measurement naming to resolve the alias-table and field-alias flow consistently for generic Telegraf payloads.
- Preserved the deterministic host-excluding `unique_key`/descriptor identity path while improving display-name quality.

### Added
- Regression coverage for the reference parser naming path and generic fallback behavior.

## [1.0.0] - 2026-08-01

### Added
- Initial Home Assistant custom integration release surface.
- MQTT subscription and parser support for Telegraf-style JSON payloads.
- Registry-backed state projection to Home Assistant sensors and binary sensors.
- Config flow and options flow support.
- HACS and Home Assistant packaging metadata alignment.
