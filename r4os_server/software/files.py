"""Descriptor-based, immutable package transfers with resumable GET requests."""
import mimetypes
import re
from urllib.parse import quote


def matches(value, etag):
    return any(part.strip().removeprefix('W/') in ('*',etag) for part in (value or '').split(','))


def byte_range(value, size):
    match=re.fullmatch(r'bytes=([0-9]{0,20})-([0-9]{0,20})',value.strip(),re.I)
    if not match or not any(match.groups()):raise ValueError('Invalid single byte range.')
    if not match[1]:
        length=int(match[2])
        if length<=0:raise ValueError('Empty suffix range.')
        return max(0,size-length),size-1
    start=int(match[1]);end=min(int(match[2]),size-1) if match[2] else size-1
    if start>=size or start>end:raise ValueError('Range is outside the file.')
    return start,end


def send(handler, package_id, asset_id):
    catalog=handler.server.catalog
    row,asset=catalog.asset(package_id,asset_id)
    with catalog.open_asset(row,asset) as stream:
        size=asset['size'];etag='"'+asset['sha256']+'"'
        if matches(handler.headers.get('If-None-Match'),etag):
            handler.send_response(304)
            handler.send_header('ETag',etag);handler.send_header('Cache-Control','private, no-cache')
            handler.send_header('Connection','close');handler.end_headers();handler.close_connection=True
            return
        requested=handler.headers.get('Range') if handler.command=='GET' else None
        if requested and handler.headers.get('If-Range') not in (None,etag):requested=None
        start,end,status=0,size-1,200
        if requested:
            try:start,end=byte_range(requested,size);status=206
            except ValueError:
                return handler.json(416,{'error':'invalid_range'},headers={'Content-Range':'bytes */'+str(size),'Accept-Ranges':'bytes','ETag':etag})
        handler.send_response(status)
        handler.send_header('Content-Type',mimetypes.guess_type(asset['name'])[0] or 'application/octet-stream')
        handler.send_header('Content-Length',str(end-start+1))
        fallback=re.sub('[^A-Za-z0-9_.-]','_',asset['name'])
        handler.send_header('Content-Disposition','attachment; filename="'+fallback+'"; filename*=UTF-8\'\''+quote(asset['name'],safe=''))
        handler.send_header('Accept-Ranges','bytes');handler.send_header('ETag',etag)
        handler.send_header('X-Checksum-SHA256',asset['sha256'])
        handler.send_header('X-Content-Type-Options','nosniff');handler.send_header('Cache-Control','private, no-cache')
        handler.send_header('Connection','close')
        if status==206:handler.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        handler.end_headers();handler.close_connection=True
        if handler.command=='HEAD':return
        stream.seek(start);remaining=end-start+1
        try:
            while remaining:
                chunk=stream.read(min(remaining,1024*1024))
                if not chunk:return  # Content-Length lets the client detect an incomplete transfer.
                handler.wfile.write(chunk);remaining-=len(chunk)
        except (OSError,TimeoutError):
            return  # An interrupted binary response must never gain an appended JSON body.
