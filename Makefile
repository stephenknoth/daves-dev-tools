# python 3.6 is used, for the time being, in order to ensure compatibility
install:
	(python3.6 -m venv venv || python3 -m venv venv) && \
	venv/bin/pip3 install --upgrade pip && \
	venv/bin/pip3 install\
	 -r requirements.txt\
	 -e '.[all]' && \
	venv/bin/pre-commit install\
	 --hook-type pre-push\
	 --hook-type pre-commit && \
	venv/bin/mypy --install-types --non-interactive

clean:
	venv/bin/daves-dev-tools uninstall-all\
	 -e '.[all]'\
     -e pyproject.toml\
     -e tox.ini\
     -e requirements.txt && \
	venv/bin/daves-dev-tools clean

distribute:
	venv/bin/daves-dev-tools distribute --skip-existing

upgrade:
	venv/bin/pre-commit autoupdate && \
	venv/bin/daves-dev-tools requirements freeze\
	 -nv '*' . pyproject.toml tox.ini \
	 > .unversioned_requirements.txt && \
	echo "pre-commit" >> .unversioned_requirements.txt && \
	venv/bin/pip3 install --upgrade --upgrade-strategy eager\
	 -r .unversioned_requirements.txt -e '.[all]' && \
	rm .unversioned_requirements.txt && \
	make requirements

requirements:
	venv/bin/daves-dev-tools requirements update\
	 -v\
	 -aen all\
	 setup.cfg pyproject.toml tox.ini && \
	echo "pre-commit" >> .unversioned_requirements.txt && \
	venv/bin/daves-dev-tools requirements freeze\
	 -nv setuptools -nv filelock -nv platformdirs\
	 '.[all]' pyproject.toml tox.ini .unversioned_requirements.txt\
	 > requirements.txt && \
	rm .unversioned_requirements.txt
