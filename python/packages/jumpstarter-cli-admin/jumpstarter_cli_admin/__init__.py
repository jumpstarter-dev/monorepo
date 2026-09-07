import click
from jumpstarter_cli_common.alias import AliasedGroup
from jumpstarter_cli_common.opt import opt_log_level
from jumpstarter_cli_common.version import version

from .apply import apply
from .create import create
from .delete import delete
from .get import get
from .import_res import import_res
from .rotate import rotate


@click.group(cls=AliasedGroup)
@opt_log_level
def admin():
    """Jumpstarter Kubernetes cluster admin CLI tool"""


admin.add_command(get)
admin.add_command(apply)
admin.add_command(create)
admin.add_command(delete)
admin.add_command(import_res)
admin.add_command(rotate)
admin.add_command(version)

if __name__ == "__main__":
    admin()
