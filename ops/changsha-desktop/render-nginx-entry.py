"""Render the complete web/desktop/download entry without touching live Nginx."""
import argparse
from pathlib import Path

GATEWAY_ALIAS='megmeet-desktop-20260918-gateway'
DOWNLOAD_ALIAS='megmeet-maimi-downloads'

def render(directory,distribution,container=False):
    gateway=(directory/'nginx-desktop.locations.conf').read_text(encoding='utf-8')
    downloads=(distribution/'nginx-maimi.locations.conf').read_text(encoding='utf-8')
    # Inherited legacy auth_request must not override the new Gateway's own IAM
    # authentication; static downloads intentionally do not receive cookies.
    import re
    pattern=r'(?m)^([ \t]*location [^\n]+ \{\n)(?![ \t]*auth_request off;)'
    gateway=re.sub(pattern,r'\1    auth_request off;\n',gateway)
    downloads=re.sub(pattern,r'\1    auth_request off;\n',downloads)
    if container:
        gateway=gateway.replace('http://127.0.0.1:18091','http://'+GATEWAY_ALIAS+':8080')
        downloads=downloads.replace('http://127.0.0.1:18092','http://'+DOWNLOAD_ALIAS+':8080')
    return ('# Generated MEGMEET entry. Include once INSIDE the exact platform-ai HTTPS server.\n'
            '# Replace overlapping legacy locations after backup; nginx -t must pass before reload.\n'
            '# A running Gateway/download service alone does not activate this public entry.\n\n'
            + gateway+'\n'+downloads)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--distribution-dir',type=Path,required=True)
    parser.add_argument('--container-nginx',action='store_true')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    source=Path(__file__).resolve().parent
    content=render(source,args.distribution_dir.resolve(),args.container_nginx)
    args.output.write_text(content,encoding='utf-8')
    print('NGINX_ENTRY_FRAGMENT: '+str(args.output.resolve()))
    print('NGINX_ENTRY_ACTIVATED: NO')

if __name__=='__main__':main()
