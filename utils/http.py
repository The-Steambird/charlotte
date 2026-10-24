import urllib3


# A refused or hung connection fails on the first try.
# Server that connects but stalls gets one more.
# Redirects are still followed as GitHub serves release assets from another host.
RETRIES = urllib3.Retry(connect=0, read=1, other=0)
