import json
from typing import List

from boto3.session import Session

from dstack._internal.core.backends.aws import AWSBackend, auth, compute
from dstack._internal.core.backends.aws.config import AWSConfig
from dstack._internal.core.errors import BackendAuthError, ComputeError, ServerClientError
from dstack._internal.core.models.backends.aws import (
    AnyAWSConfigInfo,
    AWSAccessKeyCreds,
    AWSConfigInfo,
    AWSConfigInfoWithCreds,
    AWSConfigInfoWithCredsPartial,
    AWSConfigValues,
    AWSCreds,
    AWSDefaultCreds,
    AWSStoredConfig,
)
from dstack._internal.core.models.backends.base import (
    BackendType,
    ConfigElementValue,
    ConfigMultiElement,
)
from dstack._internal.core.models.common import is_core_model_instance
from dstack._internal.server import settings
from dstack._internal.server.models import BackendModel, ProjectModel
from dstack._internal.server.services.backends.configurators.base import (
    Configurator,
    raise_invalid_credentials_error,
)

REGIONS = [
    ("US East, N. Virginia", "us-east-1"),
    ("US East, Ohio", "us-east-2"),
    ("US West, N. California", "us-west-1"),
    ("US West, Oregon", "us-west-2"),
    ("Asia Pacific, Singapore", "ap-southeast-1"),
    ("Canada, Central", "ca-central-1"),
    ("Europe, Frankfurt", "eu-central-1"),
    ("Europe, Ireland", "eu-west-1"),
    ("Europe, London", "eu-west-2"),
    ("Europe, Paris", "eu-west-3"),
    ("Europe, Stockholm", "eu-north-1"),
]
REGION_VALUES = [r[1] for r in REGIONS]
DEFAULT_REGIONS = REGION_VALUES
MAIN_REGION = "us-east-1"


class AWSConfigurator(Configurator):
    TYPE: BackendType = BackendType.AWS

    def get_default_configs(self) -> List[AWSConfigInfoWithCreds]:
        if not auth.default_creds_available():
            return []
        try:
            auth.authenticate(creds=AWSDefaultCreds(), region=MAIN_REGION)
        except BackendAuthError:
            return []
        return [
            AWSConfigInfoWithCreds(
                regions=DEFAULT_REGIONS,
                creds=AWSDefaultCreds(),
            )
        ]

    def get_config_values(self, config: AWSConfigInfoWithCredsPartial) -> AWSConfigValues:
        config_values = AWSConfigValues(regions=None)
        config_values.default_creds = (
            settings.DEFAULT_CREDS_ENABLED and auth.default_creds_available()
        )
        if config.creds is None:
            return config_values
        if (
            is_core_model_instance(config.creds, AWSDefaultCreds)
            and not settings.DEFAULT_CREDS_ENABLED
        ):
            raise_invalid_credentials_error(fields=[["creds"]])
        try:
            session = auth.authenticate(creds=config.creds, region=MAIN_REGION)
        except Exception:
            if is_core_model_instance(config.creds, AWSAccessKeyCreds):
                raise_invalid_credentials_error(
                    fields=[
                        ["creds", "access_key"],
                        ["creds", "secret_key"],
                    ]
                )
            else:
                raise_invalid_credentials_error(fields=[["creds"]])
        config_values.regions = self._get_regions_element(
            selected=config.regions or DEFAULT_REGIONS
        )
        self._check_vpc_config(
            session=session,
            config=config,
        )
        return config_values

    def create_backend(
        self, project: ProjectModel, config: AWSConfigInfoWithCreds
    ) -> BackendModel:
        if config.regions is None:
            config.regions = DEFAULT_REGIONS
        return BackendModel(
            project_id=project.id,
            type=self.TYPE.value,
            config=AWSStoredConfig(**AWSConfigInfo.__response__.parse_obj(config).dict()).json(),
            auth=AWSCreds.parse_obj(config.creds).json(),
        )

    def get_config_info(self, model: BackendModel, include_creds: bool) -> AnyAWSConfigInfo:
        config = self._get_backend_config(model)
        if include_creds:
            return AWSConfigInfoWithCreds.__response__.parse_obj(config)
        return AWSConfigInfo.__response__.parse_obj(config)

    def get_backend(self, model: BackendModel) -> AWSBackend:
        config = self._get_backend_config(model)
        return AWSBackend(config=config)

    def _get_backend_config(self, model: BackendModel) -> AWSConfig:
        return AWSConfig.__response__(
            **json.loads(model.config),
            creds=AWSCreds.parse_raw(model.auth).__root__,
        )

    def _get_regions_element(self, selected: List[str]) -> ConfigMultiElement:
        element = ConfigMultiElement(selected=selected)
        for r in REGION_VALUES:
            element.values.append(ConfigElementValue(value=r, label=r))
        return element

    def _check_vpc_config(self, session: Session, config: AWSConfigInfoWithCredsPartial):
        if config.vpc_name is not None and config.vpc_ids is not None:
            raise ServerClientError(msg="Only one of vpc_name and vpc_ids can be specified")
        regions = config.regions
        if regions is None:
            regions = DEFAULT_REGIONS
        for region in regions:
            ec2_client = session.client("ec2", region_name=region)
            try:
                compute.get_vpc_id_subnet_id_or_error(
                    ec2_client=ec2_client,
                    config=AWSConfig.parse_obj(config),
                    region=region,
                )
            except ComputeError as e:
                raise ServerClientError(e.args[0])
