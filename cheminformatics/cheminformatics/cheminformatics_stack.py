"""
A CDK Module that represents a reproducable cheminformatics-megamolbart deployment
"""

from aws_cdk import (
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_efs as efs,
    aws_autoscaling as autoscaling,
    aws_ecs_patterns as ecs_patterns,
    aws_logs as logs,
    Stack,
    RemovalPolicy,
    Duration,
)
from constructs import Construct


class CheminformaticsStack(Stack):
    """
    Cheminformatics stack which deploys all the necessary resources
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.identifier = "Cheminformatics"
        self.volume_name = "cheminformatics-data-volume"
        self.vpc = None
        self.cluster = None
        self.auto_scaling_group = None
        self.efs_filesystem = None
        self.efs_volume_configuration = None
        self.cuchem = None
        self.create_resources()

    def _create_vpc(self):

        # Create a new vpc with 2 availability zones
        if self.node.try_get_context("create_new_vpc") == "True":
            self.vpc = ec2.Vpc(
                self,
                f"{self.identifier}-VPC",
                cidr=self.node.try_get_context("cidr_block"),
                max_azs=self.node.try_get_context("number_of_azs"),
            )
        else:
            # Use existing vpc
            self.vpc = ec2.Vpc.from_lookup(
                self,
                "VPC",
                vpc_name=self.node.try_get_context("existing_vpc_name"),
            )

    def _create_gpu_capacity(self):
        commands_user_data = ec2.UserData.for_linux()

        # Make the default runtime nvidia so that multiple tasks
        # can share 1 gpu
        commands_user_data.add_commands(
            """
            sed -i 's/^OPTIONS="/OPTIONS="--default-runtime nvidia /' /etc/sysconfig/docker && systemctl restart docker
        """
        )

        auto_scaling_group = autoscaling.AutoScalingGroup(
            self,
            f"{self.identifier}-GPU-ASG",
            vpc=self.vpc,
            instance_type=ec2.InstanceType(
                self.node.try_get_context("instance_type")
            ),
            machine_image=ecs.EcsOptimizedImage.amazon_linux2(
                hardware_type=ecs.AmiHardwareType.GPU
            ),
            user_data=commands_user_data,
            min_capacity=1,
            max_capacity=2,
            block_devices=[
                autoscaling.BlockDevice(
                    device_name="/dev/xvda",
                    volume=autoscaling.BlockDeviceVolume.ebs(
                        self.node.try_get_context("ec2_volume_size")
                    ),
                ),
            ],
        )

        capacity_provider = ecs.AsgCapacityProvider(
            self,
            f"{self.identifier}-GPU-AsgCapacityProvider",
            auto_scaling_group=auto_scaling_group,
            enable_managed_termination_protection=False,
        )

        self.capacity_provider = capacity_provider
 
    def _create_ecs_cluster(self):
        cluster = ecs.Cluster(
            self,
            f"{self.identifier}-Cluster",
            vpc=self.vpc,
            container_insights=True,
        )

        # Namespace is added for container to container communication
        # aka service discovery
        cluster.add_default_cloud_map_namespace(
            name="service.local",
        )
        cluster.add_asg_capacity_provider(self.capacity_provider)

        self.cluster = cluster

    def _create_efs_volume(self):

        efs_filesystem = efs.FileSystem(
            self,
            f"{self.identifier}-EFSFilesystem",
            vpc=self.vpc,
            removal_policy=RemovalPolicy.DESTROY,
        )

        efs_volume_configuration = ecs.EfsVolumeConfiguration(
            file_system_id=efs_filesystem.file_system_id,
        )
        self.efs_filesystem = efs_filesystem
        self.efs_volume_configuration = efs_volume_configuration

    def _create_megamolbart_service(self):
        task_definition = ecs.Ec2TaskDefinition(
            self,
            "Megamolbart-Task",
            network_mode=ecs.NetworkMode.AWS_VPC,
            placement_constraints=[
                ecs.PlacementConstraint.member_of(
                    f"attribute:ecs.instance-type == {self.node.try_get_context('instance_type')}"
                )
            ],
        )

        task_definition.add_volume(
            name=self.volume_name,
            efs_volume_configuration=self.efs_volume_configuration,
        )

        init_container = task_definition.add_container(
            "megamolbart-init",
            image=ecs.ContainerImage.from_registry("ubuntu:22.04"),
            memory_limit_mib=1024,
            cpu=1024,
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix=f"{self.identifier}-db-init-",
                log_retention=logs.RetentionDays.ONE_WEEK,
            ),
            working_directory="/models/megamolbart",
            entry_point=["/bin/bash", "-c"],
            command=[
                f"apt-get update && \
                apt-get install wget unzip -y && \
                wget --content-disposition {self.node.try_get_context('megamolbart_model_url')} -nc -O megamolbart_0.1.zip && \
                unzip megamolbart_0.1.zip"
            ],
            essential=False,
        )

        init_container.add_mount_points(
            ecs.MountPoint(
                container_path="/models",
                read_only=False,
                source_volume=self.volume_name,
            )
        )

        container = task_definition.add_container(
            "megamolbart",
            image=ecs.ContainerImage.from_registry(
                self.node.try_get_context("megamolbart_container")
            ),
            memory_reservation_mib=2048,
            cpu=2048,
            port_mappings=[ecs.PortMapping(container_port=50051)],
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix=f"{self.identifier}-",
                log_retention=logs.RetentionDays.ONE_WEEK,
            ),
        )

        container.add_mount_points(
            ecs.MountPoint(
                container_path="/data",
                read_only=False,
                source_volume=self.volume_name,
            )
        )

        container.add_mount_points(
            ecs.MountPoint(
                container_path="/models",
                read_only=False,
                source_volume=self.volume_name,
            )
        )

        container.add_container_dependencies(
            ecs.ContainerDependency(
                container=init_container,
                condition=ecs.ContainerDependencyCondition.COMPLETE,
            )
        )

        megamolbart = ecs.Ec2Service(
            self,
            "megamolbart-service",
            task_definition=task_definition,
            placement_constraints=[
                ecs.PlacementConstraint.member_of(
                    f"attribute:ecs.instance-type == {self.node.try_get_context('instance_type')}"
                )
            ],
            placement_strategies=[
                ecs.PlacementStrategy.spread_across_instances(),
                ecs.PlacementStrategy.packed_by_cpu(),
            ],
            cloud_map_options=ecs.CloudMapOptions(
                name="megamolbart", container=container
            ),
            cluster=self.cluster,
            enable_execute_command=True,
        )
        
        auto_scaling = megamolbart.auto_scale_task_count(
            min_capacity=1, max_capacity=4
        )

        auto_scaling.scale_on_cpu_utilization(
            "CPUScaling",
            target_utilization_percent=50,
            scale_in_cooldown=Duration.seconds(60),
            scale_out_cooldown=Duration.seconds(60),
        )

        self.efs_filesystem.connections.allow_from(
            megamolbart, ec2.Port.tcp(2049)
        )
        megamolbart.connections.allow_from(
            self.cuchem.service, ec2.Port.all_tcp()
        )

    def _create_cuchem_service(self):
        task_definition = ecs.Ec2TaskDefinition(
            self,
            "Cuchem-Task",
            network_mode=ecs.NetworkMode.AWS_VPC,
            placement_constraints=[
                ecs.PlacementConstraint.member_of(
                    f"attribute:ecs.instance-type == {self.node.try_get_context('instance_type')}"
                )
            ],
        )

        task_definition.add_volume(
            name=self.volume_name,
            efs_volume_configuration=self.efs_volume_configuration,
        )

        init_container = task_definition.add_container(
            "cuchem-init",
            image=ecs.ContainerImage.from_registry(
                self.node.try_get_context("cheminformatics_container")
            ),
            memory_limit_mib=1024,
            cpu=1024,
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix=f"{self.identifier}-db-init-",
                log_retention=logs.RetentionDays.ONE_WEEK,
            ),
            entry_point=["/bin/bash", "-c"],
            working_directory="/opt/nvidia/cheminfomatics/setup",
            command=["source ./env.sh && dbSetup /data"],
            essential=False,
        )

        init_container.add_mount_points(
            ecs.MountPoint(
                container_path="/data",
                read_only=False,
                source_volume=self.volume_name,
            )
        )

        container = task_definition.add_container(
            "cuchem",
            image=ecs.ContainerImage.from_registry(
                self.node.try_get_context("cheminformatics_container")
            ),
            environment={"Megamolbart": "megamolbart.service.local:50051"},
            memory_reservation_mib=4096,
            cpu=1024,
            port_mappings=[ecs.PortMapping(container_port=5000)],
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix=f"{self.identifier}-",
                log_retention=logs.RetentionDays.ONE_WEEK,
            ),
        )

        container.add_mount_points(
            ecs.MountPoint(
                container_path="/data",
                read_only=False,
                source_volume=self.volume_name,
            )
        )

        container.add_container_dependencies(
            ecs.ContainerDependency(
                container=init_container,
                condition=ecs.ContainerDependencyCondition.COMPLETE,
            )
        )

        cuchem = ecs_patterns.ApplicationLoadBalancedEc2Service(
            self,
            f"{self.identifier}-Cuchem-Service",
            cluster=self.cluster,
            listener_port=80,
            load_balancer_name="cheminformatics-cuchem-lb",
            task_definition=task_definition,
            cloud_map_options=ecs.CloudMapOptions(
                name="cuchem", container=container
            ),
        )

        auto_scaling = cuchem.service.auto_scale_task_count(
            min_capacity=1, max_capacity=4
        )

        auto_scaling.scale_on_cpu_utilization(
            "CPUScaling",
            target_utilization_percent=50,
            scale_in_cooldown=Duration.seconds(60),
            scale_out_cooldown=Duration.seconds(60),
        )

        self.cuchem = cuchem
        self.efs_filesystem.connections.allow_from(
            cuchem.service, ec2.Port.tcp(2049)
        )

    def create_resources(self):
        """
        Function that orchestrates the deployment
        """
        self._create_vpc()
        self._create_gpu_capacity()
        self._create_ecs_cluster()
        self._create_efs_volume()
        self._create_cuchem_service()
        self._create_megamolbart_service()
