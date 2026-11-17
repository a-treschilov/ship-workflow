PROJECT ?= $(CURDIR)
REF ?= main
HOST ?=
MODE ?=
DEV ?=
PACKAGE := $(CURDIR)

.PHONY: install update sync check test uninstall

install update:
	@test -n "$(REF)" || (echo "usage: make $@ REF=vX.Y.Z [PROJECT=/path]" >&2; exit 2)
	cd "$(PROJECT)" && "$(PACKAGE)/install/install.sh" "$(REF)" $(if $(HOST),--host "$(HOST)") $(if $(MODE),--models "$(MODE)") $(if $(DEV),--dev)

sync:
	cd "$(PROJECT)" && python3 -I -B .agents/workflows/ship-fix/scripts/agent_models.py sync

check:
	cd "$(PROJECT)" && python3 -I -B .agents/workflows/ship-fix/scripts/agent_models.py check

test:
	cd workflows/ship-fix/scripts/tests && SHIP_TEST_TMPDIR=$$(mktemp -d) python3 -I -B -m unittest discover -s . -p 'test_*.py'

uninstall:
	cd "$(PROJECT)" && python3 -I -B "$(PACKAGE)/install/uninstall.py" "$(PROJECT)"
