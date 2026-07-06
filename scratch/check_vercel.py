import urllib.request, re
try:
    html = urllib.request.urlopen('https://semanticgateway.vercel.app/').read().decode('utf-8')
    m = re.search(r'assets/index-[a-zA-Z0-9_-]+\.js', html)
    if m:
        js_url = 'https://semanticgateway.vercel.app/' + m.group(0)
        js = urllib.request.urlopen(js_url).read().decode('utf-8')
        matches = re.findall(r'https://semanticgateway-api\.onrender\.com[^"\'`]*', js)
        local_matches = re.findall(r'http://localhost[^"\'`]*', js)
        print('Found API URLs in Vercel bundle:', set(matches))
        print('Found localhost URLs:', set(local_matches))
    else:
        print("Could not find JS bundle")
except Exception as e:
    print(e)
