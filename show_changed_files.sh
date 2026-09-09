#!/usr/bin/bash
git status . --porcelain | grep "cache/images"|  gawk '{print $2}'|xargs ls -hs|sort -rh|grep "M "
