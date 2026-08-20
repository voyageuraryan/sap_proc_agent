"""Fixed reference data: vendors, materials, plants.

Vendor names and commodity descriptions are drawn from the California open
procurement dataset (data.ca.gov). Base prices are plausible list prices.

No randomness here -- master data is fixed by definition, so there is nothing
to seed and nothing that can drift between runs. Only scenarios.py needs the
rng. Hardcoded lists rather than Faker: Faker's output changes between
versions, which would silently rewrite committed data on a dependency bump.
"""

from decimal import Decimal

from erp_domain.models import Material, Plant, Vendor

VENDORS = [
    Vendor(
        vendor_id="1000000001",
        name="3B INDUSTRIES INC",
        country="US",
    ),
    Vendor(
        vendor_id="1000000002",
        name="A&M UNIFORMS INC",
        country="US",
    ),
    Vendor(
        vendor_id="1000000003",
        name="CIRRUS ENVIRONMENTAL INC",
        country="US",
    ),
    Vendor(
        vendor_id="1000000004",
        name="CLARKE SALES",
        country="US",
    ),
    Vendor(
        vendor_id="1000000005",
        name="DEARBORN GROUP TECHNOLOGY",
        country="US",
    ),
    Vendor(
        vendor_id="1000000006",
        name="GRANITE DATA SOLUTIONS",
        country="US",
    ),
    Vendor(
        vendor_id="1000000007",
        name="NELSON & SONS INC",
        country="US",
    ),
    Vendor(
        vendor_id="1000000008",
        name="SIERRA SAFETY COMPANY",
        country="US",
    ),
    Vendor(
        vendor_id="1000000009",
        name="SMILE BUSINESS PRODUCTS INC",
        country="US",
    ),
    Vendor(
        vendor_id="1000000010",
        name="TECHNOLOGY INTEGRATION GROUP",
        country="US",
    ),
    Vendor(
        vendor_id="1000000011",
        name="WESTERN BLUE CORPORATION",
        country="US",
    ),
    Vendor(
        vendor_id="1000000012",
        name="ZUMAR INDUSTRIES",
        country="US",
    ),
]

MATERIALS = [
    Material(
        material_id="000000000000000001",
        description="BALLAST, ELECTRONIC, 4-LAMP T8",
        unit_of_measure="EA",
        base_price=Decimal("34.50"),
    ),
    Material(
        material_id="000000000000000002",
        description="BATTERY, AA ALKALINE, 24-PACK",
        unit_of_measure="PAK",
        base_price=Decimal("18.75"),
    ),
    Material(
        material_id="000000000000000003",
        description="BOLLARD, STEEL, 4IN X 36IN",
        unit_of_measure="EA",
        base_price=Decimal("212.00"),
    ),
    Material(
        material_id="000000000000000004",
        description="BOOTS, SAFETY, STEEL TOE",
        unit_of_measure="PR",
        base_price=Decimal("118.00"),
    ),
    Material(
        material_id="000000000000000005",
        description="CABLE, CAT6 UTP, 1000FT BOX",
        unit_of_measure="BOX",
        base_price=Decimal("168.00"),
    ),
    Material(
        material_id="000000000000000006",
        description="CLEANER, DEGREASER, 5GAL PAIL",
        unit_of_measure="PAI",
        base_price=Decimal("62.40"),
    ),
    Material(
        material_id="000000000000000007",
        description="CONE, TRAFFIC, 28IN REFLECTIVE",
        unit_of_measure="EA",
        base_price=Decimal("21.90"),
    ),
    Material(
        material_id="000000000000000008",
        description="COVERALL, DISPOSABLE, TYVEK",
        unit_of_measure="EA",
        base_price=Decimal("6.85"),
    ),
    Material(
        material_id="000000000000000009",
        description="DESK, OFFICE, 60IN LAMINATE",
        unit_of_measure="EA",
        base_price=Decimal("465.00"),
    ),
    Material(
        material_id="000000000000000010",
        description="DRUM, POLY, 55GAL OPEN HEAD",
        unit_of_measure="EA",
        base_price=Decimal("89.00"),
    ),
    Material(
        material_id="000000000000000011",
        description="ELBOW, STAINLESS STEEL, 2IN 90DEG",
        unit_of_measure="EA",
        base_price=Decimal("43.20"),
    ),
    Material(
        material_id="000000000000000012",
        description="EXTINGUISHER, FIRE, ABC 10LB",
        unit_of_measure="EA",
        base_price=Decimal("74.50"),
    ),
    Material(
        material_id="000000000000000013",
        description="FILTER, HEPA, 24X24X12",
        unit_of_measure="EA",
        base_price=Decimal("156.00"),
    ),
    Material(
        material_id="000000000000000014",
        description="FLANGE, WELD NECK, 3IN 150LB",
        unit_of_measure="EA",
        base_price=Decimal("58.75"),
    ),
    Material(
        material_id="000000000000000015",
        description="GASKET, SPIRAL WOUND, 4IN",
        unit_of_measure="EA",
        base_price=Decimal("27.40"),
    ),
    Material(
        material_id="000000000000000016",
        description="GLOVE, NITRILE, BOX OF 100",
        unit_of_measure="BOX",
        base_price=Decimal("14.60"),
    ),
    Material(
        material_id="000000000000000017",
        description="HARD HAT, CLASS E, WHITE",
        unit_of_measure="EA",
        base_price=Decimal("19.25"),
    ),
    Material(
        material_id="000000000000000018",
        description="HOSE, HYDRAULIC, 1/2IN 50FT",
        unit_of_measure="EA",
        base_price=Decimal("187.00"),
    ),
    Material(
        material_id="000000000000000019",
        description="KEYBOARD, USB, FULL SIZE",
        unit_of_measure="EA",
        base_price=Decimal("24.99"),
    ),
    Material(
        material_id="000000000000000020",
        description="LAPTOP, BUSINESS, 14IN",
        unit_of_measure="EA",
        base_price=Decimal("1185.00"),
    ),
    Material(
        material_id="000000000000000021",
        description="LUBRICANT, MULTIPURPOSE, 20L",
        unit_of_measure="L",
        base_price=Decimal("8.40"),
    ),
    Material(
        material_id="000000000000000022",
        description="MONITOR, LCD, 24IN 1080P",
        unit_of_measure="EA",
        base_price=Decimal("189.00"),
    ),
    Material(
        material_id="000000000000000023",
        description="MOUSE, OPTICAL, WIRED USB",
        unit_of_measure="EA",
        base_price=Decimal("12.50"),
    ),
    Material(
        material_id="000000000000000024",
        description="PAINT, TRAFFIC MARKING, WHITE 5GAL",
        unit_of_measure="PAI",
        base_price=Decimal("96.00"),
    ),
    Material(
        material_id="000000000000000025",
        description="PAPER, COPY, 20LB LETTER, CASE",
        unit_of_measure="CS",
        base_price=Decimal("46.80"),
    ),
    Material(
        material_id="000000000000000026",
        description="PIPE, PVC SCH40, 4IN X 20FT",
        unit_of_measure="EA",
        base_price=Decimal("38.90"),
    ),
    Material(
        material_id="000000000000000027",
        description="POST, SIGN, GALVANIZED 12FT",
        unit_of_measure="EA",
        base_price=Decimal("67.25"),
    ),
    Material(
        material_id="000000000000000028",
        description="PUMP, SUBMERSIBLE, 1HP",
        unit_of_measure="EA",
        base_price=Decimal("612.00"),
    ),
    Material(
        material_id="000000000000000029",
        description="RESPIRATOR, HALF MASK, P100",
        unit_of_measure="EA",
        base_price=Decimal("42.00"),
    ),
    Material(
        material_id="000000000000000030",
        description="ROUTER, ENTERPRISE, 8-PORT",
        unit_of_measure="EA",
        base_price=Decimal("845.00"),
    ),
    Material(
        material_id="000000000000000031",
        description="SHEETING, REFLECTIVE, HIP 48IN ROLL",
        unit_of_measure="RO",
        base_price=Decimal("425.00"),
    ),
    Material(
        material_id="000000000000000032",
        description="SIGN, STOP, 30IN OCTAGON",
        unit_of_measure="EA",
        base_price=Decimal("88.50"),
    ),
    Material(
        material_id="000000000000000033",
        description="SOLVENT, ACETONE, 1GAL",
        unit_of_measure="GAL",
        base_price=Decimal("31.75"),
    ),
    Material(
        material_id="000000000000000034",
        description="SWITCH, NETWORK, 24-PORT GIGABIT",
        unit_of_measure="EA",
        base_price=Decimal("398.00"),
    ),
    Material(
        material_id="000000000000000035",
        description="TAPE, BARRICADE, 3IN X 1000FT",
        unit_of_measure="RO",
        base_price=Decimal("11.30"),
    ),
    Material(
        material_id="000000000000000036",
        description="TONER CARTRIDGE, BLACK, HP 26A",
        unit_of_measure="EA",
        base_price=Decimal("78.99"),
    ),
    Material(
        material_id="000000000000000037",
        description="TONER CARTRIDGE, MAGENTA, HP 202A",
        unit_of_measure="EA",
        base_price=Decimal("112.50"),
    ),
    Material(
        material_id="000000000000000038",
        description="UNIFORM SHIRT, WORK, LONG SLEEVE",
        unit_of_measure="EA",
        base_price=Decimal("32.75"),
    ),
    Material(
        material_id="000000000000000039",
        description="VALVE, BALL, BRASS 1IN NPT",
        unit_of_measure="EA",
        base_price=Decimal("26.40"),
    ),
    Material(
        material_id="000000000000000040",
        description="VEST, SAFETY, CLASS 2 HI-VIS",
        unit_of_measure="EA",
        base_price=Decimal("15.80"),
    ),
]

PLANTS = [
    Plant(
        plant_id="1000",
        name="SACRAMENTO DISTRIBUTION CENTER",
    ),
    Plant(
        plant_id="1100",
        name="FRESNO MAINTENANCE YARD",
    ),
    Plant(
        plant_id="2000",
        name="LOS ANGELES WAREHOUSE",
    ),
    Plant(
        plant_id="2100",
        name="OAKLAND FLEET DEPOT",
    ),
]


def _take(roster: list, count: int | None, what: str) -> list:
    """Return the first `count` entries, or all of them when count is None.

    Raises rather than capping. Asking for 20 vendors and silently getting 12
    is the kind of quiet disagreement between config and code that turns into
    an unexplainable eval result three weeks later.
    """
    if count is None:
        return list(roster)
    if count > len(roster):
        raise ValueError(
            f"config asks for {count} {what} but only {len(roster)} are defined "
            f"in master_data.py"
        )
    return roster[:count]


def build_vendors(count: int | None = None) -> list[Vendor]:
    return _take(VENDORS, count, "vendors")


def build_materials(count: int | None = None) -> list[Material]:
    return _take(MATERIALS, count, "materials")


def build_plants() -> list[Plant]:
    """No count parameter -- three plants is not a knob worth having."""
    return list(PLANTS)
