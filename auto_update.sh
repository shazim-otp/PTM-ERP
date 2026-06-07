#!/bin/bash

echo "=== $(date) ===" >> ~/NAS/PTM/deploy.log

cd ~/NAS/PTM

git pull origin main >> ~/NAS/PTM/deploy.log 2>&1

source venv/bin/activate

pip install -r requirements.txt >> ~/NAS/PTM/deploy.log 2>&1

echo "Deployment finished" >> ~/NAS/PTM/deploy.log
