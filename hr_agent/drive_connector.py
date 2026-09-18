"""Drive v3 adapter. Never changes sharing permissions. OAuth stays on disk, mode 0600."""
from pathlib import Path
import io
import json
import os
import time
import tempfile
import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ['https://www.googleapis.com/auth/drive']
BASE = 'https://www.googleapis.com/drive/v3/files'
UPLOAD = 'https://www.googleapis.com/upload/drive/v3/files'
FIELDS = 'id,name,mimeType,modifiedTime,md5Checksum,size,parents,trashed,appProperties,version'
CV_MIME = {'.pdf':'application/pdf',
           '.docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
           '.txt':'text/plain'}

class DriveError(RuntimeError):
    pass

class SourceChanged(DriveError):
    pass

def authorize(config):
    flow = InstalledAppFlow.from_client_secrets_file(str(config.credentials), SCOPES)
    creds = flow.run_local_server(host='127.0.0.1',port=0,access_type='offline',prompt='consent')
    save_token(config.data/'token.json',creds)

def save_token(path,creds):
    # Scanner and worker refresh independently. A shared .tmp path can corrupt a token
    # or disappear underneath another writer. Each atomic replacement owns its file.
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix='.oauth-',suffix='.tmp',dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd,'w') as file:
            file.write(creds.to_json())
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp,path)
    finally:
        temp.unlink(missing_ok=True)

class Drive:
    def __init__(self,config):
        self.config = config
        path = config.data/'token.json'
        if not path.exists():
            raise DriveError('Google sign-in required: run hr-agent auth')
        self.creds = Credentials.from_authorized_user_file(path,SCOPES)
        self.session = AuthorizedSession(self.creds)

    def request(self,method,url=BASE,**kwargs):
        response = self.session.request(method,url,timeout=(10,self.config.timeout),**kwargs)
        if self.creds.token:
            save_token(self.config.data/'token.json',self.creds)
        if response.status_code >= 400:
            # Do not log request bodies, tokens or CV text.
            raise DriveError(f'Drive returned HTTP {response.status_code}')
        return response

    def list(self,parent):
        token = None
        while True:
            params = {'q':f"'{parent.replace(chr(39), '')}' in parents and trashed=false",
                      'fields':f'nextPageToken,files({FIELDS})','pageSize':100,
                      'supportsAllDrives':'true','includeItemsFromAllDrives':'true'}
            if token:
                params['pageToken'] = token
            result = self.request('GET',params=params).json()
            yield from result.get('files',[])
            token = result.get('nextPageToken')
            if not token:
                break

    def get(self,file_id):
        return self.request('GET',f'{BASE}/{file_id}',params={'fields':FIELDS,'supportsAllDrives':'true'}).json()

    def assert_private(self,file_id):
        permissions=[]
        token=None
        while True:
            params={'fields':'nextPageToken,permissions(type,role,emailAddress,domain)', 'supportsAllDrives':'true','pageSize':100}
            if token:
                params['pageToken']=token
            result=self.request('GET',f'{BASE}/{file_id}/permissions',params=params).json()
            permissions.extend(result.get('permissions',[]))
            token=result.get('nextPageToken')
            if not token:
                break
        if not permissions:
            raise DriveError('Cannot verify HR-only folder access')
        for permission in permissions:
            if permission.get('type') in {'anyone','domain'}:
                raise DriveError('Recruitment/report access is public or domain-wide; restrict it to HR before synchronization')
            if permission.get('role')!='owner' and permission.get('emailAddress','').lower() not in self.config.hr_principals:
                raise DriveError('Folder has a principal outside HR_ALLOWED_PRINCIPALS; verify HR access configuration')

    def reserve_id(self):
        return self.request('GET',f'{BASE}/generateIds',params={'count':1,'space':'drive','type':'files'}).json()['ids'][0]

    def create_folder(self,parent,name,file_id):
        try:
            return self.request('POST',json={'id':file_id,'name':name,'mimeType':'application/vnd.google-apps.folder','parents':[parent]},
                                params={'fields':FIELDS,'supportsAllDrives':'true'}).json()
        except DriveError:
            found = self.get(file_id)
            if found.get('name') != name or parent not in found.get('parents',[]):
                raise
            return found

    def download(self,file_id,path,expected_version=None):
        metadata = self.get(file_id)
        if expected_version and content_version(metadata) != expected_version:
            raise SourceChanged('Source changed before download; waiting for next scan')
        limit = self.config.max_file_mb*1024*1024
        if int(metadata.get('size',0)) > limit:
            raise ValueError('File exceeds configured size limit')
        response = self.request('GET',f'{BASE}/{file_id}',params={'alt':'media','supportsAllDrives':'true'},stream=True)
        total = 0
        started = time.monotonic()
        temp = Path(str(path)+'.part')
        try:
            with response, temp.open('wb') as out:
                for chunk in response.iter_content(65536):
                    total += len(chunk)
                    if total > limit or time.monotonic()-started > self.config.timeout:
                        raise ValueError('Download exceeded file/time limit')
                    out.write(chunk)
            if expected_version and content_version(self.get(file_id)) != expected_version:
                raise SourceChanged('Source changed during download')
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)

    def upload_report(self,file_id,parent,path,role_id,revision):
        self.assert_private(self.config.root)
        self.assert_private(parent)
        props = {'hrRole':role_id,'hrRevision':str(revision)}
        # A reserved ID is saved before the request. Uncertain creates retry that exact ID.
        try:
            existing = self.get(file_id)
        except DriveError as exc:
            if 'HTTP 404' not in str(exc):
                raise
            existing = None
        if existing:
            self.assert_private(file_id)
        if existing and int(existing.get('appProperties',{}).get('hrRevision','-1')) >= revision:
            return existing
        metadata = {'name':'candidates.xlsx','appProperties':props}
        if existing is None:
            metadata.update(id=file_id,parents=[parent])
        boundary = 'hr_agent_multipart_boundary'
        body = (f'--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'.encode()
                +json.dumps(metadata).encode()+f'\r\n--{boundary}\r\nContent-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n'.encode()
                +Path(path).read_bytes()+f'\r\n--{boundary}--\r\n'.encode())
        url = 'https://www.googleapis.com/upload/drive/v3/files'+('/'+file_id if existing else '')
        self.request('PATCH' if existing else 'POST',url,data=body,
                     headers={'Content-Type':f'multipart/related; boundary={boundary}'},
                     params={'uploadType':'multipart','supportsAllDrives':'true'})
        confirmed = self.get(file_id)
        if confirmed.get('appProperties',{}).get('hrRevision') != str(revision):
            raise DriveError('Report revision confirmation failed')
        return confirmed

    def upload_cv(self,parent,filename,data):
        """Place an HR-supplied CV into a role's Incoming CVs folder for ordinary discovery.

        Stamped appProperties.hrUpload for provenance, but deliberately NOT hrRole:
        the scanner skips hrRole files as generated reports, so a CV must not carry it
        or it would never be discovered. Refuses a public/domain-wide folder and never
        alters sharing, mirroring upload_report's privacy stance for sensitive content.
        """
        self.assert_private(self.config.root)
        self.assert_private(parent)
        limit = self.config.max_file_mb*1024*1024
        if len(data)>limit:
            raise ValueError('File exceeds configured size limit')
        file_id = self.reserve_id()
        mime = CV_MIME.get(Path(filename).suffix.lower(),'application/octet-stream')
        metadata = {'id':file_id,'name':filename,'parents':[parent],'appProperties':{'hrUpload':'1'}}
        boundary = 'hr_agent_multipart_boundary'
        body = (f'--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'.encode()
                +json.dumps(metadata).encode()+f'\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n'.encode()
                +data+f'\r\n--{boundary}--\r\n'.encode())
        self.request('POST',UPLOAD,data=body,headers={'Content-Type':f'multipart/related; boundary={boundary}'},
                     params={'uploadType':'multipart','supportsAllDrives':'true'})
        confirmed = self.get(file_id)
        if parent not in confirmed.get('parents',[]) or confirmed.get('appProperties',{}).get('hrRole'):
            raise DriveError('Upload could not be confirmed in the Incoming CVs folder')
        return confirmed

    def move(self,file_id,target):
        self.assert_private(target)
        self.assert_private(file_id)
        current = self.get(file_id)
        parents = current.get('parents',[])
        if target in parents:
            return
        params = {'addParents':target,'fields':FIELDS,'supportsAllDrives':'true'}
        if parents:
            params['removeParents'] = ','.join(parents)
        self.request('PATCH',f'{BASE}/{file_id}',params=params,json={})
        if target not in self.get(file_id).get('parents',[]):
            raise DriveError('Move could not be confirmed')

def content_version(metadata):
    # modifiedTime/version also change on moves. md5Checksum tracks binary content only.
    return metadata.get('md5Checksum') or metadata.get('modifiedTime') or str(metadata.get('version','unknown'))
