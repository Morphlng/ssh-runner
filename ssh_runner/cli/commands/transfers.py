"""File and directory transfer commands."""

from .base import Command, output_arguments, remote_arguments, values


class TransferCommand(Command):
    def __init__(self, directory, action):
        self.directory, self.action = directory, action
        self.path = ("directory" if directory else "file", action)
        self.help = ("上传" if action == "upload" else "下载") + (
            "目录到新目标" if directory else "文件"
        )
        self.requirements = (
            "Python 3.8+, POSIX, atomic rename-no-replace"
            if directory
            else "POSIX sh, SHA-256 tools"
        )

    def configure(self, parser):
        parser.add_argument("source")
        parser.add_argument("destination")
        remote_arguments(parser)
        if not self.directory:
            output_arguments(parser, files=False)
            parser.add_argument("--overwrite", action="store_true")
            if self.action == "upload":
                parser.add_argument(
                    "--mode",
                    default="600",
                    choices=("600", "640", "644", "700", "750", "755"),
                )

    def run(self, context, options):
        service = context.client.directories if self.directory else context.client.files
        kwargs = values(options, "timeout")
        if not self.directory:
            kwargs.update(values(options, "overwrite", "max_output_bytes"))
            if self.action == "upload":
                kwargs["mode"] = options.mode
        return getattr(service, self.action)(
            options.source, options.destination, **kwargs
        )
