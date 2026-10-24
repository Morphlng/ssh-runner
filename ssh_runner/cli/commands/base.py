"""Small command interface and shared argparse declarations."""

from pathlib import Path

from ...core.defaults import COMMAND_TIMEOUT, DEFAULT_OUTPUT_BYTES


class Command:
    path = ()
    help = ""
    requirements = "local"

    @property
    def operation(self):
        return ".".join(self.path)

    def configure(self, parser):
        pass

    def validate(self, options):
        pass

    def run(self, context, options):
        raise NotImplementedError


def remote_arguments(parser):
    parser.add_argument(
        "--timeout", type=float, default=COMMAND_TIMEOUT, help="单次 SSH 请求超时秒数"
    )
    parser.add_argument(
        "--control-dir", type=Path, help="连接状态目录（复用模式包括控制 socket）"
    )
    parser.add_argument("--no-reuse", action="store_true", help="本次使用独立短连接")


def output_arguments(parser, files=True):
    parser.add_argument(
        "--max-output-bytes",
        type=int,
        default=DEFAULT_OUTPUT_BYTES,
        help="每路输出预览字节上限",
    )
    if files:
        parser.add_argument(
            "--stdout-file", type=Path, help="完整 stdout 保存到新的本地文件"
        )
        parser.add_argument(
            "--stderr-file", type=Path, help="完整 stderr 保存到新的本地文件"
        )


def values(options, *names):
    return {name: getattr(options, name) for name in names}
