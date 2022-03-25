# python 3.6 is used, for the time being, in order to ensure compatibility
install:
	{ python3.6 -m venv venv || python3 -m venv venv || \
	py -3.6 -m venv venv || py -3 -m venv venv ; } && \
	{ venv/Scripts/activate.bat || . venv/bin/activate ; } && \
	python3 -m pip install --upgrade pip pre-commit && \
	python3 -m pip install -r requirements.txt -e '.[all]' && \
	pre-commit install\
	 --hook-type pre-push --hook-type pre-commit && \
	mypy --install-types --non-interactive ; \
	echo "Installation complete"

editable:
	{ venv/Scripts/activate.bat || . venv/bin/activate ; } && \
	daves-dev-tools install-editable --upgrade-strategy eager && \
	make requirements

clean:
	{ venv/Scripts/activate.bat || . venv/bin/activate ; } && \
	daves-dev-tools uninstall-all\
	 -e '.[all]'\
     -e pyproject.toml\
     -e tox.ini\
     -e requirements.txt && \
	daves-dev-tools clean

distribute:
	{ venv/Scripts/activate.bat || . venv/bin/activate ; } && \
	daves-dev-tools distribute --skip-existing

upgrade:
	{ venv/Scripts/activate.bat || . venv/bin/activate ; } && \
	pre-commit autoupdate && \
	daves-dev-tools requirements freeze\
	 -nv '*' . pyproject.toml tox.ini \
	 > .unversioned_requirements.txt && \
	python3 -m pip install --upgrade --upgrade-strategy eager\
	 -r .unversioned_requirements.txt -e '.[all]' && \
	rm .unversioned_requirements.txt && \
	make requirements

requirements:
	{ venv/Scripts/activate.bat || . venv/bin/activate ; } && \
	daves-dev-tools requirements update\
	 -v\
	 -aen all\
	 setup.cfg pyproject.toml tox.ini && \
	daves-dev-tools requirements freeze\
	 -nv setuptools -nv filelock -nv platformdirs\
	 '.[all]' pyproject.toml tox.ini\
	 > requirements.txt

test:
	{ venv/Scripts/activate.bat || . venv/bin/activate ; } && tox -r
