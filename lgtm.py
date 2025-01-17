from dataclasses import dataclass
from typing import Optional, List, Dict, Callable

import requests
import yaml
import time

from requests.exceptions import SSLError


class LGTMRequestException(Exception):
    pass


@dataclass
class LGTMSite:
    nonce: str
    long_session: str
    short_session: str
    api_version: str

    def _cookies(self):
        return {
            'lgtm_long_session': self.long_session,
            'lgtm_short_session': self.short_session
        }

    def _headers(self):
        return {
            'LGTM-Nonce': self.nonce
        }

    def _make_lgtm_get(self, url: str) -> dict:
        r = LGTMSite._resilient_request(lambda: requests.get(
            url,
            cookies=self._cookies(),
            headers=self._headers()
        ))
        return r.json()

    def get_my_projects(self) -> List[dict]:
        url = 'https://lgtm.com/internal_api/v0.2/getMyProjects?apiVersion=' + self.api_version
        data = self._make_lgtm_get(url)
        if data['status'] == 'success':
            return data['data']
        else:
            raise LGTMRequestException('LGTM GET request failed with response: %s' % str(data))

    def get_my_projects_under_org(self, org: str) -> List['SimpleProject']:
        projects_sorted = LGTMDataFilters.org_to_ids(self.get_my_projects())
        return LGTMDataFilters.extract_project_under_org(org, projects_sorted)

    def _make_lgtm_post(self, url: str, data: dict, retry_count: int = 0) -> dict:
        api_data = {
            'apiVersion': self.api_version
        }
        full_data = {**api_data, **data}
        print(data)
        r = LGTMSite._resilient_request(lambda: requests.post(
            url,
            full_data,
            cookies=self._cookies(),
            headers=self._headers()
        ))
        try:
            data_returned = r.json()
        except ValueError as e:
            response_text = r.text
            raise LGTMRequestException(f'Failed to parse JSON. Response was: {response_text}') from e

        print(data_returned)
        if data_returned['status'] == 'success':
            if 'data' in data_returned:
                return data_returned['data']
            else:
                return {}
        else:
            raise LGTMRequestException('LGTM POST request failed with response: %s' % str(data_returned))

    def load_into_project_list(self, into_project: int, lgtm_project_ids: List[str]):
        url = "https://lgtm.com/internal_api/v0.2/updateProjectSelection"
        # Because LGTM uses some wacky format for it's application/x-www-form-urlencoded data
        list_serialized = ', '.join([('"' + str(elem) + '"') for elem in lgtm_project_ids])
        data = {
            'projectSelectionId': into_project,
            'addedProjects': '[' + list_serialized + ']',
            'removedProjects': '[]',
        }
        self._make_lgtm_post(url, data)

    def force_rebuild_all_proto_projects(self):
        org_to_projects = LGTMDataFilters.org_to_ids(self.get_my_projects())
        for org in org_to_projects:
            for project in org_to_projects[org]:
                if not project.is_protoproject:
                    continue

                time.sleep(1)
                self.force_rebuild_project(project)

    def force_rebuild_project(self, simple_project: 'SimpleProject'):
        url = 'https://lgtm.com/internal_api/v0.2/rebuildProtoproject'
        data = {
            **simple_project.make_post_data(),
            'config': ''
        }
        try:
            self._make_lgtm_post(url, data)
        except LGTMRequestException:
            print('Failed rebuilding project. This may be because it is already being built. `%s`' % simple_project)

    def follow_repository(self, repository_url: str):
        url = "https://lgtm.com/internal_api/v0.2/followProject"
        data = {
            'url': repository_url,
            'apiVersion': self.api_version
        }
        self._make_lgtm_post(url, data)

    def unfollow_repository_by_id(self, project_id: str):
        url = "https://lgtm.com/internal_api/v0.2/unfollowProject"
        data = {
            'project_key': project_id,
        }
        self._make_lgtm_post(url, data)

    def unfollow_repository(self, simple_project: 'SimpleProject'):
        url = "https://lgtm.com/internal_api/v0.2/unfollowProject" if not simple_project.is_protoproject \
            else "https://lgtm.com/internal_api/v0.2/unfollowProtoproject"
        data = simple_project.make_post_data()
        self._make_lgtm_post(url, data)

    def unfollow_repository_by_org(self, org: str, include_protoproject: bool = False):
        projects_under_org = self.get_my_projects_under_org(org)
        for project in projects_under_org:
            if not include_protoproject and project.is_protoproject:
                print("Not unfollowing project since it is a protoproject. %s" % project)
                continue
            print('Unfollowing project %s' % project.display_name)
            self.unfollow_repository(project)

    def get_project_lists(self):
        url = 'https://lgtm.com/internal_api/v0.2/getUsedProjectSelections'
        return self._make_lgtm_post(url, {})

    def get_project_list_by_name(self, list_name: str) -> Optional[int]:
        project_lists = self.get_project_lists()
        for project_list in project_lists:
            if project_list['name'] == list_name:
                return int(project_list['key'])
        return None

    def get_or_create_project_list(self, list_name: str) -> int:
        project_list_id = self.get_project_list_by_name(list_name)
        if project_list_id is not None:
            print('Found Project List with name: %s' % list_name)
        else:
            print('Creating Project List with name: %s' % list_name)
            project_list_id = self.create_project_list(list_name)
        return project_list_id

    def create_project_list(self, name: str) -> int:
        """
        :param name: Name of the project list to create.
        :return: The key id for this project.
        """
        url = 'https://lgtm.com/internal_api/v0.2/createProjectSelection'
        data = {
            'name': name
        }
        response = self._make_lgtm_post(url, data)
        return int(response['key'])

    def add_org_to_project_list_by_list_key(self, org: str, project_list_key: int):
        projects_under_org = self.get_my_projects_under_org(org)
        ids = []
        for project in projects_under_org:
            print('Adding `%s` project to project list' % project.display_name)
            ids.append(project.key)
        self.load_into_project_list(project_list_key, ids)

    def add_org_to_project_list_by_list_name(self, org: str, project_name: str):
        pass

    @staticmethod
    def _resilient_request(request_method: Callable[[], requests.Response], retry_count: int = 0):
        try:
            return request_method()
        except SSLError as e:
            if retry_count < 4:
                return LGTMSite._resilient_request(request_method, retry_count + 1)
            raise LGTMRequestException(f'SSL Error') from e

    @staticmethod
    def retrieve_project(gh_project_path: str):
        url = "https://lgtm.com/api/v1.0/projects/g/" + gh_project_path
        r = LGTMSite._resilient_request(lambda: requests.get(url))
        return r.json()

    @staticmethod
    def retrieve_project_id(gh_project_path: str) -> Optional[int]:
        data_returned = LGTMSite.retrieve_project(gh_project_path)
        if 'id' in data_returned:
            return int(data_returned["id"])
        else:
            return None

    @staticmethod
    def create_from_file() -> 'LGTMSite':
        with open("config.yml") as config_file:
            config = yaml.safe_load(config_file)
            lgtm: dict = config['lgtm']
            return LGTMSite(
                nonce=lgtm['nonce'],
                long_session=lgtm['long_session'],
                short_session=lgtm['short_session'],
                api_version=lgtm['api_version'],
            )


@dataclass
class SimpleProject:
    display_name: str
    key: str
    is_protoproject: bool

    def make_post_data(self):
        data_dict_key = 'protoproject_key' if self.is_protoproject else 'project_key'
        return {
            data_dict_key: self.key
        }


class LGTMDataFilters:

    @staticmethod
    def org_to_ids(projects: List[Dict]) -> Dict[str, List[SimpleProject]]:
        """
        Converts the output from :func:`~lgtm.LGTMSite.get_my_projects` into a dic of GH org
        to list of projects including their GH id and LGTM id.
        """
        org_to_ids = {}
        for project in projects:
            org: str
            display_name: str
            key: str
            is_protoproject: bool
            if 'protoproject' in project:
                the_project = project['protoproject']
                if 'https://github.com/' not in the_project['cloneUrl']:
                    # Not really concerned with BitBucket right now
                    continue
                display_name = the_project['displayName']
                org = display_name.split('/')[0]
                key = the_project['key']
                is_protoproject = True
            elif 'realProject' in project:

                the_project = project['realProject'][0]
                if the_project['repoProvider'] != 'github_apps':
                    # Not really concerned with BitBucket right now
                    continue
                org = str(the_project['slug']).split('/')[1]
                display_name = the_project['displayName']
                key = the_project['key']
                is_protoproject = False
            else:
                raise KeyError('\'realProject\' nor \'protoproject\' in %s' % str(project))

            ids_list: List[SimpleProject]
            if org in org_to_ids:
                ids_list = org_to_ids[org]
            else:
                ids_list = []
                org_to_ids[org] = ids_list
            ids_list.append(SimpleProject(
                display_name=display_name,
                key=key,
                is_protoproject=is_protoproject
            ))

        return org_to_ids

    @staticmethod
    def extract_project_under_org(org: str, projects_sorted: Dict[str, List[SimpleProject]]) -> List[SimpleProject]:
        if org not in projects_sorted:
            print('org %s not found in projects list' % org)
            return []
        return projects_sorted[org]
