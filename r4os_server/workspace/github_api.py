"""Small GitHub Releases transport with bounded reads and scoped credentials."""
import hashlib
from http.client import HTTPSConnection
import json
from pathlib import Path
from urllib.parse import quote, urlsplit


class GitHubFailure(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Reader:
    def __init__(self, stream, check):
        self.stream, self.check = stream, check

    def read(self, size):
        self.check()
        return self.stream.read(size)


class Client:
    def __init__(self, repo, token, check=lambda: None):
        self.repo, self.token, self.check = repo, token, check
        self.base = '/repos/'+quote(repo['owner'], safe='')+'/'+quote(repo['display_name'], safe='')

    def headers(self, accept='application/vnd.github+json'):
        return {'User-Agent': 'R4OS-Server', 'Accept': accept, 'Authorization': 'Bearer '+self.token,
                'X-GitHub-Api-Version': '2026-03-10'}

    @staticmethod
    def failure(status):
        messages = {401: 'GitHub rejected the connection token.',
                    403: 'GitHub denied access. Check token Contents/Workflows permissions and API rate limits.',
                    404: 'GitHub could not find this resource or the token cannot access it.',
                    409: 'GitHub reported a release conflict.',
                    422: 'This GitHub tag, release or asset name is already occupied or invalid.',
                    429: 'GitHub API rate limit reached. Retry later.'}
        return GitHubFailure(status, messages.get(status, 'GitHub is unavailable (HTTP '+str(status)+').'))

    def request(self, method, suffix, body=None, *, file=None, upload=False, missing=False):
        self.check()
        connection = HTTPSConnection('uploads.github.com' if upload else 'api.github.com', timeout=40)
        headers = self.headers()
        stream = None
        try:
            if file is not None:
                path = Path(file)
                if not 0 < path.stat().st_size <= 2*1024**3:
                    raise ValueError('GitHub assets must be nonempty and at most 2 GiB.')
                stream = path.open('rb')
                payload = Reader(stream, self.check)
                headers.update({'Content-Type': 'application/octet-stream', 'Content-Length': str(path.stat().st_size)})
            else:
                payload = None if body is None else json.dumps(body).encode()
                if payload is not None: headers['Content-Type'] = 'application/json'
            connection.request(method, self.base+suffix, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read(16*1024**2+1)
            if response.status == 404 and missing:
                return None
            if not 200 <= response.status < 300:
                raise self.failure(response.status)
            if len(raw) > 16*1024**2:
                raise GitHubFailure(502, 'The GitHub response is too large.')
            return json.loads(raw) if raw else None
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise GitHubFailure(503, 'The GitHub connection failed. Local build files have been retained.') from None
        finally:
            if stream: stream.close()
            connection.close()

    def tag_commit(self, tag):
        value = self.request('GET', '/git/ref/tags/'+quote(tag, safe=''), missing=True)
        if value is None: return None
        obj = value['object']
        seen = set()
        while obj['type'] == 'tag':
            if obj['sha'] in seen or len(seen) >= 8:
                raise GitHubFailure(422, 'The GitHub tag cannot be resolved to a commit.')
            seen.add(obj['sha'])
            obj = self.request('GET', '/git/tags/'+quote(obj['sha'], safe=''))['object']
        if obj['type'] != 'commit':
            raise GitHubFailure(422, 'The GitHub tag does not identify a source commit.')
        return obj['sha']

    def assets(self, release):
        result = []
        for page in range(1, 101):
            items = self.request('GET', '/releases/'+str(int(release['id']))+'/assets?per_page=100&page='+str(page))
            if not isinstance(items, list): raise GitHubFailure(502, 'Invalid GitHub release asset list.')
            result.extend(items)
            if len(items) < 100: return result
        raise GitHubFailure(502, 'Too many GitHub release assets.')

    def _asset(self, asset, limit, digest):
        url = 'https://api.github.com'+self.base+'/releases/assets/'+str(int(asset['id']))
        hashed, output, size = hashlib.sha256(), [], 0
        for redirect in range(4):
            parsed = urlsplit(url)
            if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443):
                raise GitHubFailure(502, 'Unexpected GitHub asset address.')
            if parsed.hostname not in ('api.github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'):
                raise GitHubFailure(502, 'Unexpected GitHub asset download host.')
            headers = self.headers('application/octet-stream') if redirect == 0 else {'User-Agent': 'R4OS-Server'}
            connection = HTTPSConnection(parsed.hostname, timeout=40)
            try:
                self.check()
                connection.request('GET', parsed.path+('?' + parsed.query if parsed.query else ''), headers=headers)
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    url = response.getheader('Location', '')
                    continue
                if response.status != 200: raise self.failure(response.status)
                while chunk := response.read(65536):
                    self.check()
                    size += len(chunk)
                    if size > limit: raise GitHubFailure(502, 'GitHub asset exceeds the expected size.')
                    if digest: hashed.update(chunk)
                    else: output.append(chunk)
                if size != int(asset['size']): raise GitHubFailure(502, 'The GitHub asset is incomplete.')
                return hashed.hexdigest() if digest else b''.join(output)
            except OSError:
                raise GitHubFailure(503, 'The GitHub asset could not be read. Retry later.') from None
            finally: connection.close()
        raise GitHubFailure(502, 'Too many GitHub asset redirects.')

    def asset_bytes(self, asset, limit=131072):
        return self._asset(asset, limit, False)

    def asset_digest(self, asset):
        digest = asset.get('digest') or ''
        if digest.startswith('sha256:') and len(digest) == 71:
            return digest[7:]
        return self._asset(asset, min(int(asset['size']), 2*1024**3), True)

    def upload(self, release, name, path):
        return self.request('POST', '/releases/'+str(int(release['id']))+'/assets?name='+quote(name, safe=''), file=path, upload=True)
