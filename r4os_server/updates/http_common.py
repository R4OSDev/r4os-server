"""Compatibility methods for the reused range-download implementation."""
from ..common.http import Handler


class UpdateRequestHandler(Handler):
    def client_source_ip(self):
        return self.source_ip()

    def send_bytes(self,status,body,content_type,head_only=False):
        self.reply(status,body,content_type)

    def send_text(self,status,text,head_only=False):
        self.reply(status,text.encode(),"text/plain; charset=utf-8")

    def send_json(self,status,value,head_only=False):
        self.json(status,value)
