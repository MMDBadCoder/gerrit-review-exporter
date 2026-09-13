#!/bin/bash
set -euo pipefail
site=/tmp/export-test-site
if [ ! -d "$site/git/All-Projects.git" ]; then
  java -Xmx512m -jar /var/gerrit/bin/gerrit.war init --batch --dev --no-auto-start -d "$site"
fi
git config -f "$site/etc/gerrit.config" gerrit.canonicalWebUrl http://localhost:18080/
git config -f "$site/etc/gerrit.config" httpd.listenUrl http://*:8080/
git config -f "$site/etc/gerrit.config" sshd.listenAddress '*:29418'
git config -f "$site/etc/gerrit.config" sendemail.enable false
git config -f "$site/etc/gerrit.config" auth.type DEVELOPMENT_BECOME_ANY_ACCOUNT
exec java -Xms256m -Xmx768m -jar /var/gerrit/bin/gerrit.war daemon -d "$site" --console-log
