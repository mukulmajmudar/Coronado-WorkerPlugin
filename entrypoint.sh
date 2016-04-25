#!/bin/bash
python3 setup.py bdist_egg
chown -R $USERID dist
chgrp -R $USERID dist
