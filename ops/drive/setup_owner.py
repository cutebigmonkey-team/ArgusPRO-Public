"""One-time owner setup. No parser, local database, token output, or automatic sharing."""
import argparse
import json
from pathlib import Path
import os
import subprocess
import sys
import html

from drive_store import DriveStore, SCOPES, service_from_info

def secure_write(path, text):
    path=Path(path).resolve()
    if path.exists(): raise ValueError('Choose a new credential output file')
    path.parent.mkdir(parents=True,exist_ok=True)
    if os.name=='nt':
        identity=subprocess.run(['whoami'],check=True,text=True,capture_output=True).stdout.strip()
        subprocess.run(['icacls',str(path.parent),'/inheritance:r','/grant:r',
                        identity+':(OI)(CI)F','*S-1-5-18:(OI)(CI)F'],check=True,stdout=subprocess.DEVNULL)
    else: path.parent.chmod(0o700)
    with path.open('x',encoding='utf-8') as out: out.write(text)
    if os.name!='nt': path.chmod(0o600)

def main():
    from google_auth_oauthlib.flow import InstalledAppFlow
    parser=argparse.ArgumentParser()
    parser.add_argument('--client',required=True,help='Downloaded Desktop OAuth client JSON')
    parser.add_argument('--output',required=True,help='NEW file in a dedicated private folder outside the repository')
    parser.add_argument('--folder',help='Existing Argus folder ID, or create a new private Argus archive folder')
    parser.add_argument('--expected-email',help='Require this Google account before saving credentials or creating a folder')
    parser.add_argument('--authorization-file',help='Write a temporary HTML start link instead of opening the default browser')
    args=parser.parse_args()
    output=Path(args.output).resolve()
    repo=Path(__file__).resolve().parents[2]
    if output.is_relative_to(repo) or output.exists():
        raise ValueError('Credentials must be a new file outside the repository')
    client=json.loads(Path(args.client).read_text(encoding='utf-8-sig'))
    info=client.get('installed',{})
    if info.get('auth_uri')!='https://accounts.google.com/o/oauth2/auth' or info.get('token_uri')!='https://oauth2.googleapis.com/token':
        raise ValueError('Use an official Google Desktop OAuth client file')
    class OwnerFlow(InstalledAppFlow):
        def authorization_url(self, **kwargs):
            url,state=super().authorization_url(**kwargs)
            if args.authorization_file:
                secure_write(args.authorization_file,
                    '<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
                    '<title>Argus Google authorization</title><h1>Argus Google Drive</h1>'
                    '<p>Choose '+html.escape(args.expected_email or 'your intended account')
                    +' in Google.</p><a rel="noreferrer" href="'
                    +html.escape(url,quote=True)+'">Continue to Google</a>')
            return url,state
    flow=OwnerFlow.from_client_config(client,SCOPES)
    # Google presents the account picker; the owner chooses the NEW account.
    # The loopback listener exists only during this authorization, never as an Argus backend.
    oauth_options={'login_hint':args.expected_email} if args.expected_email else {}
    credentials=flow.run_local_server(host='127.0.0.1',port=0,open_browser=not args.authorization_file,
        authorization_prompt_message='Complete authorization in your browser. Tokens are never printed.',
        success_message='Argus authorization completed. You can close this tab.',
        access_type='offline',prompt='consent select_account',timeout_seconds=600,**oauth_options)
    if not credentials.refresh_token: raise ValueError('Google did not issue offline access')
    saved={'client_id':info['client_id'],'client_secret':info['client_secret'],
           'refresh_token':credentials.refresh_token,'type':'authorized_user'}
    service=service_from_info(saved)
    if args.expected_email:
        account=service.about().get(fields='user(emailAddress)').execute(num_retries=3)
        if account.get('user',{}).get('emailAddress','').casefold()!=args.expected_email.casefold():
            raise ValueError('The selected Google account does not match the required account')
    # Save before the first Drive write, so a subsequent setup failure does not lose consent.
    secure_write(output,json.dumps(saved))
    folder=args.folder
    if not folder:
        folder=service.files().create(body={'name':'Argus Archive','mimeType':'application/vnd.google-apps.folder'},fields='id').execute(num_retries=3)['id']
    store=DriveStore(service,folder);store.assert_private()
    print('Private Argus folder ID:',folder)
    print('OAuth credentials saved in the chosen protected file. Do not paste them into chat or commit them.')
    print('Add that JSON as the GitHub Actions secret ARGUS_DRIVE_OAUTH; folder ID as variable ARGUS_DRIVE_FOLDER_ID.')

if __name__=='__main__':
    try: main()
    except Exception as error:
        print('Setup failed ('+type(error).__name__+'). No token was printed.',file=sys.stderr);sys.exit(1)
