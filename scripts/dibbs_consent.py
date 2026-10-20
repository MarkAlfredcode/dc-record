#!/usr/bin/env python3
"""Accept the DoD notice-and-consent banner on both DIBBS hosts and leave the cookies in a curl jar.
Usage: dibbs_consent.py <cookie-jar>   (each host has its own banner and cookie)"""
import html, re, subprocess, sys, urllib.parse

JAR = sys.argv[1]
HOSTS = {'www.dibbs.bsm.dla.mil': '/', 'dibbs2.bsm.dla.mil': '/Downloads/RFQ/Archive/in000000.txt'}


def curl(*a):
    return subprocess.run(['curl', '-sS', '--retry', '4', '--retry-all-errors', '-m', '60', '-c', JAR, '-b', JAR, *a],
                          capture_output=True, check=True).stdout


for host, goto in HOSTS.items():
    page = curl(f'https://{host}/dodwarning.aspx?goto={urllib.parse.quote(goto)}').decode('latin-1')
    if 'butAgree' not in page:
        sys.exit(f'{host}: no consent form found (layout changed or blocked?): {page[:200]!r}')
    pairs = []
    for m in re.finditer(r'<input[^>]*type="hidden"[^>]*>', page, re.I):
        n = re.search(r'name="([^"]*)"', m.group(0)).group(1)
        v = re.search(r'value="([^"]*)"', m.group(0))
        pairs.append((n, html.unescape(v.group(1)) if v else ''))
    pairs.append(('butAgree', 'OK'))
    action = re.search(r'<form[^>]*action="([^"]*)"', page).group(1)
    url = urllib.parse.urljoin(f'https://{host}/dodwarning.aspx', html.unescape(action))
    curl('-o', '/dev/null', '--data', urllib.parse.urlencode(pairs), url)
    print(f'consent accepted on {host}')
