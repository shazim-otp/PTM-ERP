#!/bin/bash

cd ~/NAS/PTM

git pull origin main

source venv/bin/activate

pip install -r requirements.txt

touch app.py
