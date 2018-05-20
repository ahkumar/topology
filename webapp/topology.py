from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from enum import Enum
import pprint
import re
import urllib.parse
import sys
from typing import Dict, List

import dateparser

from .common import MaybeOrderedDict, RGDOWNTIME_SCHEMA_URL, RGSUMMARY_SCHEMA_URL, Filters,\
    is_null, expand_attr_list_single, expand_attr_list, ensure_list


GRIDTYPE_1 = "OSG Production Resource"
GRIDTYPE_2 = "OSG Integration Test Bed Resource"

class Timeframe(Enum):
    PAST = 1
    PRESENT = 2
    FUTURE = 3


class TopologyError(Exception): pass


class Tables(object):
    """Global data, e.g. various mappings"""
    def __init__(self, contacts: Dict, service_types: Dict, support_centers: Dict):
        self.contacts = contacts
        self.service_types = service_types
        self.support_centers = support_centers


class Facility(object):
    def __init__(self, name: str, id: int):
        self.name = name
        self.id = id

    def get_tree(self) -> OrderedDict:
        return OrderedDict([("ID", self.id), ("Name", self.name)])


class Site(object):
    # probably will have some other attributes like address, latitude, longitude, etc.
    def __init__(self, name: str, id: int, facility: Facility):
        self.name = name
        self.id = id
        self.facility = facility

    def get_tree(self) -> OrderedDict:
        return OrderedDict([("ID", self.id), ("Name", self.name)])


class Resource(object):
    def __init__(self, name: str, yaml_data: Dict, tables: Tables):
        self.name = name
        self.service_types = tables.service_types
        self.tables = tables
        if not is_null(yaml_data, "Services"):
            self.services = self._expand_services(yaml_data["Services"])
        else:
            print("{0} does not have any services".format(name), file=sys.stderr)
            self.services = []
        self.data = yaml_data

    def get_tree(self, authorized=False, filters: Filters = None) -> MaybeOrderedDict:
        if filters is None:
            filters = Filters()

        defaults = {
            "ContactLists": None,
            "FQDNAliases": None,
            "VOOwnership": "(Information not available)",
            "WLCGInformation": "(Information not available)",
        }

        res = dict(self.data)

        if filters.active is not None and res["Active"] != filters.active:
            return
        if filters.disable is not None and res["Disable"] != filters.disable:
            return

        if filters.service_id:
            filtered_services = [svc for svc in self.services if svc["ID"] in filters.service_id]
            if not filtered_services:
                return  # all services filtered out
            res["Services"] = {"Service": filtered_services}
        else:
            res["Services"] = {"Service": self.services}

        if "VOOwnership" in res:
            res["VOOwnership"] = self._expand_voownership(res["VOOwnership"])
        if "FQDNAliases" in res:
            res["FQDNAliases"] = {"FQDNAlias": res["FQDNAliases"]}
        if not is_null(res, "ContactLists"):
            res["ContactLists"] = self._expand_contactlists(res["ContactLists"], authorized)
        res["Name"] = self.name
        if "WLCGInformation" in res and isinstance(res["WLCGInformation"], dict):
            res["WLCGInformation"] = self._expand_wlcginformation(res["WLCGInformation"])

        new_res = OrderedDict()
        for elem in ["ID", "Name", "Active", "Disable", "Services", "Description", "FQDN", "FQDNAliases", "VOOwnership",
                     "WLCGInformation", "ContactLists"]:
            if elem in res:
                new_res[elem] = res[elem]
            elif elem in defaults:
                new_res[elem] = defaults[elem]

        return new_res

    def _expand_services(self, services: Dict) -> List[OrderedDict]:
        services_list = expand_attr_list(services, "Name", ordering=["Name", "Description", "Details"])
        for svc in services_list:
            svc["ID"] = self.service_types[svc["Name"]]
            svc.move_to_end("ID", last=False)
        return services_list

    @staticmethod
    def _expand_voownership(voownership: Dict) -> OrderedDict:
        """Return the data structure for an expanded VOOwnership for a single Resource."""

        def _get_charturl(ownership):
            # Return a URL for a pie chart based on (VO, Percent) pairs.
            chd = ""
            chl = ""

            for name, percent in ownership:
                chd += "{0},".format(percent)
                if name == "(Other)":
                    name = "Other"
                chl += "{0}({1}%)|".format(percent, name)
            chd = chd.rstrip(",")
            chl = chl.rstrip("|")

            query = urllib.parse.urlencode({
                "chco": "00cc00",
                "cht": "p3",
                "chd": "t:" + chd,
                "chs": "280x65",
                "chl": chl
            })
            return "http://chart.apis.google.com/chart?" + query

        voo = voownership.copy()
        totalpercent = sum(voo.values())
        if totalpercent < 100:
            voo["(Other)"] = 100 - totalpercent
        return OrderedDict([
            ("Ownership", expand_attr_list_single(voo, "VO", "Percent", name_first=False)),
            ("ChartURL", _get_charturl(voownership.items()))
        ])

    def _expand_contactlists(self, contactlists: Dict, authorized: bool) -> Dict:
        """Return the data structure for an expanded ContactLists for a single Resource."""
        new_contactlists = []
        for contact_type, contact_data in contactlists.items():
            contact_data = expand_attr_list(contact_data, "ContactRank", ["Name", "ID", "ContactRank"], ignore_missing=True)
            for contact in contact_data:
                contact_id = contact.pop("ID", None)  # ID is for internal use - don't put it in the results
                if authorized:
                    if contact_id in self.tables.contacts:
                        extra_data = self.tables.contacts[contact_id]
                        contact["Email"] = extra_data["Email"]
                        contact["Phone"] = extra_data.get("Phone", "")
                        contact["SMSAddress"] = extra_data.get("SMS", "")
                        contact.move_to_end("ContactRank", last=True)
            new_contactlists.append(
                OrderedDict([("ContactType", contact_type), ("Contacts", {"Contact": contact_data})]))
        return {"ContactList": new_contactlists}

    @staticmethod
    def _expand_wlcginformation(wlcg: Dict) -> OrderedDict:
        defaults = {
            "AccountingName": None,
            "InteropBDII": False,
            "LDAPURL": None,
            "TapeCapacity": 0,
        }

        new_wlcg = OrderedDict()
        for elem in ["InteropBDII", "LDAPURL", "InteropMonitoring", "InteropAccounting", "AccountingName", "KSI2KMin",
                     "KSI2KMax", "StorageCapacityMin", "StorageCapacityMax", "HEPSPEC", "APELNormalFactor",
                     "TapeCapacity"]:
            if elem in wlcg:
                new_wlcg[elem] = wlcg[elem]
            elif elem in defaults:
                new_wlcg[elem] = defaults[elem]
        return new_wlcg


class ResourceGroup(object):
    def __init__(self, name: str, yaml_data: Dict, site: Site, tables: Tables):
        self.name = name
        self.site = site
        self.service_types = tables.service_types
        self.tables = tables

        scname = yaml_data["SupportCenter"]
        scid = tables.support_centers[scname]
        self.support_center = OrderedDict([("ID", scid), ("Name", scname)])

        self.resources = []
        for name, res in yaml_data["Resources"].items():
            try:
                assert isinstance(res, dict)
                res = Resource(name, res, self.tables)
                self.resources.append(res)
            except Exception:
                pprint.pprint(res, stream=sys.stderr)
                raise
        self.resources.sort(key=lambda x: x.name)

        self.data = yaml_data

    def get_tree(self, authorized=False, filters: Filters = None) -> MaybeOrderedDict:
        if filters is None:
            filters = Filters()
        if filters.facility_id and self.site.facility.id not in filters.facility_id:
            return
        if filters.site_id and self.site.id not in filters.site_id:
            return
        if filters.grid_type is not None and self.data["GridType"] != filters.grid_type:
            return
        if filters.support_center_id:
            if int(self.support_center["ID"]) not in filters.support_center_id:
                return

        filtered_resources = list(filter(None, [x.get_tree(authorized, filters) for x in self.resources]))
        if not filtered_resources:
            return  # all resources filtered out
        filtered_data = self._expand_rg()
        filtered_data["Resources"] = {"Resource": filtered_resources}
        return filtered_data

    @property
    def id(self):
        return self.data["GroupID"]

    @property
    def key(self):
        return (self.site.name, self.name)

    def _expand_rg(self) -> OrderedDict:
        rg = dict(self.data)  # copy

        rg["Facility"] = self.site.facility.get_tree()
        rg["Site"] = self.site.get_tree()
        rg["GroupName"] = self.name
        rg["SupportCenter"] = self.support_center

        new_rg = OrderedDict()

        for elem in ["GridType", "GroupID", "GroupName", "Disable", "Facility", "Site", "SupportCenter",
                     "GroupDescription"]:
            if elem in rg:
                new_rg[elem] = rg[elem]

        return new_rg


class Downtime(object):
    def __init__(self, rg: ResourceGroup, yaml_data: Dict):
        self.rg = rg
        self.data = yaml_data
        self.start_time = self._parsetime(yaml_data["StartTime"])
        self.end_time = self._parsetime(yaml_data["EndTime"])

    @property
    def timeframe(self) -> Timeframe:
        current_time = datetime.now(timezone.utc)
        # ^ not to be confused with datetime.utcnow(), which does not include tz info in the result

        if self.end_time < current_time:
            return Timeframe.PAST
        elif self.start_time > current_time:
            return Timeframe.FUTURE
        else:
            return Timeframe.PRESENT

    @property
    def end_age(self) -> timedelta:
        current_time = datetime.now(timezone.utc)
        return self.end_time - current_time

    def get_tree(self, filters: Filters = None) -> MaybeOrderedDict:
        if filters is None:
            filters = Filters()
        if filters.facility_id and self.rg.site.facility.id not in filters.facility_id:
            return
        if filters.site_id and self.rg.site.id not in filters.site_id:
            return
        if filters.grid_type is not None and self.rg.data["GridType"] != filters.grid_type:
            return
        if filters.support_center_id:
            if int(self.rg.support_center["ID"]) not in filters.support_center_id:
                return
        # unlike the other filters, if past_days is not specified, _no_ past downtime is shown
        if filters.past_days >= 0:
            if self.end_age.total_seconds() // 86400 < filters.past_days:
                return

        return self._expand_downtime(filters.service_id)

    def _expand_downtime(self, service_filter=None) -> MaybeOrderedDict:
        new_downtime = OrderedDict.fromkeys(["ID", "ResourceID", "ResourceGroup", "ResourceName", "ResourceFQDN",
                                             "StartTime", "EndTime", "Class", "Severity", "CreatedTime", "UpdateTime",
                                             "Services", "Description"])
        new_downtime["ResourceGroup"] = OrderedDict([("GroupName", self.rg.name),
                                                     ("GroupID", self.rg.id)])
        for r in self.rg.resources:
            if r.name == self.data["ResourceName"]:
                new_downtime["ResourceFQDN"] = r.data["FQDN"]
                new_downtime["ResourceID"] = r.data["ID"]
                new_downtime["ResourceName"] = r.name
                services = ensure_list(r.services)
                break
        else:
            # print("Resource %s does not exist" % downtime["ResourceName"], file=sys.stderr)
            return None

        new_services = []
        for dts in self.data["Services"]:
            for s in services:
                if s["Name"] == dts:
                    if not service_filter or s["ID"] in service_filter:
                        new_services.append(OrderedDict([
                            ("ID", s["ID"]),
                            ("Name", s["Name"]),
                            ("Description", s["Description"])
                        ]))
                    break
            else:
                pass

        if new_services:
            new_downtime["Services"] = {"Service": new_services}
        else:
            return None

        new_downtime["CreatedTime"] = "Not Available"
        new_downtime["UpdateTime"] = "Not Available"

        for k in ["ID", "StartTime", "EndTime", "Class", "Severity", "Description"]:
            new_downtime[k] = self.data[k]

        return new_downtime

    @staticmethod
    def _parsetime(time_str: str) -> datetime:
        # get rid of stupid times like "00:00 AM" or "17:00 PM"
        if re.search(r"\s+00:\d\d\s+AM", time_str):
            time_str = time_str.replace(" AM", "")
        elif re.search(r"\s+(1[3-9]|2[0-3]):\d\d\s+PM", time_str):
            time_str = time_str.replace(" PM", "")
        time = dateparser.parse(time_str)
        if not time:
            raise ValueError("Invalid time {0}".format(time_str))
        if not time.tzinfo:
            time = time.replace(tzinfo=timezone.utc)
        return time


class Topology(object):
    def __init__(self, tables: Tables):
        self.downtimes_by_timeframe = {
            Timeframe.PAST: [],
            Timeframe.PRESENT: [],
            Timeframe.FUTURE: []}
        self.tables = tables
        self.facilities = {}
        self.sites = {}
        # rgs are keyed by (site_name, rg_name) tuple
        self.rgs = {}

    def add_rg(self, facility_name, site_name, name, parsed_data):
        if facility_name not in self.facilities:
            raise TopologyError("Unknown facility {0} -- call add_facility first".format(facility_name))
        if site_name not in self.sites:
            raise TopologyError("Unknown site {0} in facility {1} -- call add_site first".format(site_name, facility_name))
        if (site_name, name) in self.rgs:
            raise TopologyError("Duplicate RG {0} in site {1}".format(name, site_name))
        self.rgs[(site_name, name)] = ResourceGroup(name, parsed_data, self.sites[site_name], self.tables)

    def add_facility(self, name, id):
        if name in self.facilities:
            raise TopologyError("Duplicate facility " + name)
        self.facilities[name] = Facility(name, id)

    def add_site(self, facility_name, name, id):
        if facility_name not in self.facilities:
            raise TopologyError("Unknown facility {0} -- call add_facility first".format(facility_name))
        if name in self.sites:
            raise TopologyError("Duplicate site " + name)
        self.sites[name] = Site(name, id, self.facilities[facility_name])

    def get_resource_summary(self, authorized=False, filters: Filters = None) -> Dict:
        if filters is None:
            filters = Filters()
        rglist = []
        for rgkey in sorted(self.rgs.keys(), key=lambda x: x[1].lower()):
            rgval = self.rgs[rgkey]
            assert isinstance(rgval, ResourceGroup)
            rgtree = rgval.get_tree(authorized, filters)
            if rgtree:
                rglist.append(rgtree)
        return {"ResourceSummary":
                {"@xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
                 "@xsi:schemaLocation": RGSUMMARY_SCHEMA_URL,
                 "ResourceGroup": rglist}}

    def get_downtimes(self, authorized=False, filters: Filters = None) -> Dict:
        _ = authorized
        if filters is None:
            filters = Filters()

        tree = {"Downtimes": {"@xsi:schemaLocation": RGDOWNTIME_SCHEMA_URL,
                              "@xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance"}}

        for treekey, dtkey in [("PastDowntimes", Timeframe.PAST),
                               ("CurrentDowntimes", Timeframe.PRESENT),
                               ("FutureDowntimes", Timeframe.FUTURE)]:
            dtlist = list(
                filter(None,
                       [dt.get_tree(filters) for dt in self.downtimes_by_timeframe[dtkey]]))
            tree["Downtimes"][treekey] = {
                "Downtime": dtlist}

        return tree

    def add_downtime(self, sitename: str, rgname: str, downtime: Dict):
        dt = Downtime(self.rgs[(sitename, rgname)], downtime)
        self.downtimes_by_timeframe[dt.timeframe].append(dt)