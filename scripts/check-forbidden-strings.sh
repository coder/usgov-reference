#!/usr/bin/env bash
# scripts/check-forbidden-strings.sh
#
# Backward-compatibility shim. The canonical scanner is now
# scripts/check-identifiers.sh, which uses generic regex patterns instead
# of a literal denylist. All arguments are forwarded unchanged.
exec "$(dirname "${BASH_SOURCE[0]}")/check-identifiers.sh" "$@"
