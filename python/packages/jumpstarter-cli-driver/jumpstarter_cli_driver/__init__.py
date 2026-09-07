import click
from jumpstarter_cli_common.alias import AliasedGroup
from jumpstarter_cli_common.opt import opt_log_level
from jumpstarter_cli_common.version import version

from .driver import driver_schema, list_drivers


@click.group(cls=AliasedGroup)
@opt_log_level
def driver():
    """Jumpstarter driver CLI tool"""


driver.add_command(list_drivers)
driver.add_command(driver_schema)
driver.add_command(version)

if __name__ == "__main__":
    driver()
