# pylint: disable=attribute-defined-outside-init
import errno
import logging
import socket
import alarmdecoder.event as ADE

from .Signal import Signal

LOG = logging.getLogger(__name__)


class Client:
    # Alarmdecoder signals to implement the device api.  Also need write().
    on_open = ADE.event.Event("Connected event.  f(link)")
    on_close = ADE.event.Event("Close event.  f(link)")
    on_read = ADE.event.Event("Read cb.  f(link, bytes)")
    on_write = ADE.event.Event("Written db.  f(link, data)")

    def __init__(self, host='', port=10000, reconnect_dt=30,
                 commands_enabled=False):
        self.signal_closing = Signal()
        self.signal_connected = Signal()
        self.signal_needs_write = Signal()
        self.addr = (host, port)
        self.reconnect_dt = reconnect_dt
        self.commands_enabled = commands_enabled
        self._init_vars()

    #-----------------------------------------------------------------------
    def authorize_write(self, data, on_sent=None):
        """Authorize one exact, one-shot panel write from Bridge."""
        if not self.commands_enabled:
            LOG.warning("Panel write authorization denied in read-only mode")
            return None

        if not isinstance(data, bytes):
            data = data.encode("utf-8")
        if not data:
            return None
        if self._authorized_write is not None:
            LOG.error("Panel transport already has a pending authorization")
            return None

        token = object()
        self._authorized_write = (token, data, on_sent)
        return token

    #-----------------------------------------------------------------------
    def cancel_write(self, token):
        """Discard a write's unsent bytes; report if no prefix was sent."""
        if (self._authorized_write is not None
                and self._authorized_write[0] == token):
            self._authorized_write = None
            return True

        offset = 0
        for index, segment in enumerate(self._write_segments):
            if segment["token"] != token:
                offset += segment["remaining"]
                continue

            wholly_unsent = not segment["sent"]
            end = offset + segment["remaining"]
            self._write_buf = self._write_buf[:offset] + self._write_buf[end:]
            self._write_segments.pop(index)
            if not self._write_buf:
                self.signal_needs_write.emit(self, False)
            if not wholly_unsent:
                LOG.error(
                    "Discarded %s buffered bytes after a partial panel write; "
                    "operator recovery required",
                    segment["remaining"],
                )
            return wholly_unsent

        return False

    #-----------------------------------------------------------------------
    def write(self, data):
        if not len(data):
            return

        if not isinstance(data, bytes):
            data = data.encode("utf-8")

        token = None
        on_sent = None
        if data not in (b"C\r", b"V\r"):
            if not self.commands_enabled:
                LOG.warning(
                    "Blocked %s outbound AlarmDecoder bytes in read-only "
                    "mode",
                    len(data),
                )
                return

            authorization = self._authorized_write
            if authorization is None or authorization[1] != data:
                LOG.warning(
                    "Blocked %s unauthorized AlarmDecoder bytes",
                    len(data),
                )
                return

            token, _authorized_data, on_sent = authorization
            self._authorized_write = None

        LOG.debug("Adding %s bytes to write buffer of %s bytes",
                  len(data), len(self._write_buf))
        self._write_buf += data
        self._write_segments.append({
            "token": token,
            "remaining": len(data),
            "sent": 0,
            "on_sent": on_sent,
        })

        # Only need to emit if there was no data in the buffer already.
        self.signal_needs_write.emit(self, True)

    #-----------------------------------------------------------------------
    def poll(self, t):
        """Restore write interest if data was queued before Manager.add()."""
        if self._write_buf:
            self.signal_needs_write.emit(self, True)

    #-----------------------------------------------------------------------
    def connect(self):
        """Connect the link to ser2sock.

        Returns:
          bool:  Returns True if the connection was successful or False if
          it failed.
        """
        LOG.info("Connecting to %s:%s", *self.addr)
        try:
            # ponytail: blocking connect stalls the loop <=10s; switch to
            # loop.sock_connect if that ever matters.
            self.socket = socket.create_connection(self.addr, timeout=10)
            self.socket.setblocking(False)
        except OSError:
            LOG.exception("Failed to connect")
            if self.socket:
                self.socket.close()

            self.socket = None
            return False

        LOG.info("Connected")
        self.signal_connected.emit(self, True)
        self.on_open()
        return True

    #-----------------------------------------------------------------------
    def fileno(self):
        """Return the file descriptor to watch for this link.

        Returns:
          int:  Returns the descriptor (obj.fileno() usually) to monitor.
        """
        return self.socket.fileno()

    #-----------------------------------------------------------------------
    def read_from_link(self):
        """Read data from the link.

        This will be called by the manager when there is data available on
        the file descriptor for reading.

        Returns:
           int:  Return -1 if the link had an error.  Or any other integer
           to indicate success.
        """
        LOG.debug("Reading from alarmdecoder")
        try:
            buf = self.socket.recv(4096)
        except socket.error as e:
            if e.errno in [errno.EWOULDBLOCK, errno.ETIMEDOUT]:
                return 0

            # Any other socket error is terminal.  This runs as an asyncio
            # add_reader callback: a raise would leave the fd registered and
            # the supervisor never reconnecting, so close instead.
            LOG.exception("Error during read")
            self.close()
            return -1

        # If no data was read, the connection was closed.
        if len(buf) == 0:
            self.close()
            return -1

        LOG.debug("Adding %s bytes to read buffer of %d bytes",
                  len(buf), len(self._read_buf))
        self._read_buf += buf
        self.parse_read_buf()
        return 0

    #-----------------------------------------------------------------------
    def parse_read_buf(self):
        """TODO
        """
        while True:
            line, _sep, after = self._read_buf.partition(b"\n")

            # If we didn't find a new line, or the parsed data is empty, wait
            # for more data
            if not _sep or not line:
                break

            line = line.rstrip(b"\r\n")

            # Payload-free logging (handoff G1): never log raw panel lines.
            LOG.debug("Processing %d byte line", len(line))
            self.on_read(data=line)
            self._read_buf = after

    #-----------------------------------------------------------------------
    def write_to_link(self, t):
        """Write data from the link.

        This will be called by the manager when the file descriptor can be
        written to.  It will only be called after the link as emitted the
        signal_needs_write(True).  Once all the data has been written, the
        link should call self.signal_needs_write.emit(False).

        Args:
           t (float):  The current time (time.time).
        """
        num = 0
        LOG.debug("Writing to device")
        try:
            if self._write_buf:
                num = self.socket.send(self._write_buf)
        except socket.error as e:
            # If we can't actually write, then return and try again later.
            if e.errno in [errno.EWOULDBLOCK, errno.ETIMEDOUT]:
                return

            # Any other socket error (ECONNRESET, EPIPE, ENOTCONN,
            # ECONNABORTED, ...) is terminal.  This runs as an asyncio
            # add_writer callback: a raise would leave the writable fd
            # registered and re-fire the failing callback forever, so close
            # to emit signal_closing and let the supervisor reconnect.
            LOG.exception("Error during write")
            self.close()
            return

        LOG.debug("Wrote %d bytes", num)
        if num:
            self.on_write(data=self._write_buf[:num])
            self._write_buf = self._write_buf[num:]
            self._consume_write_segments(num)

        if not len(self._write_buf):
            self.signal_needs_write.emit(self, False)

    #-----------------------------------------------------------------------
    def _consume_write_segments(self, count):
        callbacks = []
        while count and self._write_segments:
            segment = self._write_segments[0]
            consumed = min(count, segment["remaining"])
            segment["remaining"] -= consumed
            segment["sent"] += consumed
            count -= consumed
            if segment["remaining"]:
                continue

            self._write_segments.pop(0)
            if segment["on_sent"] is not None:
                callbacks.append((segment["on_sent"], segment["token"]))

        for callback, token in callbacks:
            try:
                callback(token)
            except Exception:
                LOG.exception("Panel write completion callback failed")

    #-----------------------------------------------------------------------
    def close(self):
        """Close the link.

        The link must call self.signal_closing.emit() after closing.
        """
        if self.isClosing or self.socket is None:
            return

        LOG.info("Closing device")
        self.isClosing = True
        self.signal_closing.emit(self)
        self.on_close()

        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except socket.error:
            pass

        self.socket.close()
        self._init_vars()

    #-----------------------------------------------------------------------
    def _init_vars(self):
        "TODO:"
        self.socket = None
        self._read_buf = bytes()
        self._write_buf = bytes()
        self._write_segments = []
        self._authorized_write = None
        self.isClosing = False

    #-----------------------------------------------------------------------
