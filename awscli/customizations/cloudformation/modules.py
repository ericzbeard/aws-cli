# Copyright 2012-2015 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.

from awscli.customizations.cloudformation import exceptions
from awscli.customizations.cloudformation import yamlhelper
import os

RESOURCES = "Resources"
METADATA = "Metadata"
OVERRIDES = "Overrides"
DEPENDSON = "DependsOn"
PROPERTIES = "Properties"
CREATIONPOLICY = "CreationPolicy"
UPDATEPOLICY = "UpdatePolicy"
DELETIONPOLICY = "DeletionPolicy"
UPDATEREPLACEPOLICY = "UpdateReplacePolicy"
CONDITION = "Condition"
DEFAULT = "Default"
NAME = "Name"
SOURCE = "Source"
REF = "Ref"
SUB = "Fn::Sub"
GETATT = "Fn::GetAtt"
PARAMETERS = "Parameters"


def read_source(source):
    "Read the source file and return the content as a string"

    if not isinstance(source, str) or not os.path.isfile(source):
        raise exceptions.InvalidModulePathError(source=source)

    with open(source, "r") as s:
        return s.read()


def merge_props(original, overrides):
    """
    Merge props merges dicts, replacing values in the original with overrides.
    This function is recursive and can act on lists and scalars.

    :return A new value with the overridden properties

    Original:

    A:
      B: foo
      C:
        D: bar

    Override:

    A:
      B: baz
      C:
        E: qux

    Result:

    A:
      B: baz
      C:
        D: bar
        E: qux
    """
    original_type = type(original)
    override_type = type(overrides)
    if override_type is not dict and override_type is not list:
        return overrides

    if original_type is not override_type:
        return overrides

    if original_type is dict:
        retval = original.copy()
        for k in original:
            if k in overrides:
                retval[k] = merge_props(retval[k], overrides[k])
        for k in overrides:
            if k not in original:
                retval[k] = overrides[k]
        return retval
    else:
        return original + overrides


class Module:
    """
    Process client-side modules.

    See tests/unit/customizations/cloudformation/modules for examples of what
    the Modules section of a template looks like.

    A module is itself basically a CloudFormation template, with a Parameters
    section and Resources that are injected into the parent template. The
    Properties defined in the Modules section correspond to the Parameters in
    the module. These modules operate in a similar way to registry modules.

    The name of the module in the Modules section is used as a prefix to
    logical ids that are defined in the module.

    In addition to the parent setting Properties, all attributes of the module
    can be overridden with Overrides, which require the consumer to know how
    the module is structured. This "escape hatch" is considered a first class
    citizen in the design, to avoid excessive Parameter definitions to cover
    every possible use case.

    Module Parameters (set by Properties in the parent) are handled with
    Refs, Subs, and GetAtts in the module. These are handled in a way that
    fixes references to match module prefixes, fully resolving values
    that are actually strings and leaving others to be resolved at deploy time.

    Modules can contain other modules, with no limit to the levels of nesting.
    """

    def __init__(self, template, module_config):
        """
        Initialize the module with values from the parent template

        :param template The parent template dictionary
        :param module_config The configuration from the parent Modules section
        """

        # The parent template dictionary
        self.template = template

        # The name of the module, which is used as a logical id prefix
        self.name = module_config[NAME]

        # The location of the source for the module, a URI string
        self.source = module_config[SOURCE]

        # The Properties from the parent template
        self.props = {}
        if PROPERTIES in module_config:
            self.props = module_config[PROPERTIES]

        # The Overrides from the parent template
        self.overrides = {}
        if OVERRIDES in module_config:
            self.overrides = module_config[OVERRIDES]

        # Resources defined in the module
        self.resources = {}

        # Parameters defined in the module
        self.params = {}

    def __str__(self):
        "Print out a string with module details for logs"
        return (
            f"module name: {self.name}, "
            + f"source: {self.source}, props: {self.props}"
        )

    def process(self):
        """
        Read the module source process it.

        :return: The modified parent template dictionary
        """

        content = read_source(self.source)

        module_dict = yamlhelper.yaml_parse(content)
        if RESOURCES not in module_dict:
            msg = "Modules must have a Resources section"
            raise exceptions.InvalidModuleError(msg=msg)
        self.resources = module_dict[RESOURCES]

        if PARAMETERS in module_dict:
            self.params = module_dict[PARAMETERS]

        self.validate_overrides()

        for logical_id, resource in self.resources.items():
            self.process_resource(logical_id, resource)

        return self.template

    def validate_overrides(self):
        "Make sure resources referenced by overrides actually exist"
        pass  # TODO

    def process_resource(self, logical_id, resource):
        "Process a single resource"

        # For each property (and property-like attribute),
        # replace the value if it appears in parent overrides.
        attrs = [
            PROPERTIES,
            CREATIONPOLICY,
            METADATA,
            UPDATEPOLICY,
            DELETIONPOLICY,
            CONDITION,
            UPDATEREPLACEPOLICY,
            DEPENDSON,
        ]
        for a in attrs:
            self.process_overrides(logical_id, resource, a)

        # Resolve refs, subs, and getatts
        #    (Process module Parameters and parent Properties)
        self.resolve(logical_id, resource)

        self.template[RESOURCES][self.name + logical_id] = resource

    def process_overrides(self, logical_id, resource, attr_name):
        """
        Replace overridden values in a property-like attribute of a resource.

        (Properties, Metadata, CreationPolicy, and UpdatePolicy)

        Overrides are a way to customize modules without needing a Parameter.

        Example template.yaml:

        Modules:
          Foo:
            Source: ./module.yaml
            Overrides:
              Bar:
                Properties:
                  Name: bbb

        Example module.yaml:

        Resources:
          Bar:
            Type: A::B::C
            Properties:
              Name: aaa

        Output yaml:

        Resources:
          Bar:
            Type: A::B::C
            Properties:
              Name: bbb
        """

        if logical_id not in self.overrides:
            return

        resource_overrides = self.overrides[logical_id]
        if attr_name not in resource_overrides:
            return

        # Might be overriding something that's not in the module at all,
        # like a Bucket with no Properties
        if attr_name not in resource:
            if attr_name in resource_overrides:
                resource[attr_name] = resource_overrides[attr_name]
            else:
                return

        original = resource[attr_name]
        overrides = resource_overrides[attr_name]
        resource[attr_name] = merge_props(original, overrides)

    def resolve(self, k, v):
        """
        Resolve Refs, Subs, and GetAtts recursively.

        :param k The name of the node
        :param v The value of the node

        """
        if k == REF:
            self.resolve_ref(v)
        elif k == SUB:
            self.resolve_sub(v)
        elif k == GETATT:
            self.resolve_getatt(v)
        else:
            if type(v) is dict:
                for k2, v2 in v.items():
                    self.resolve(k2, v2)
            elif type(v) is list:
                for v2 in v:
                    if type(v2) is dict:
                        for k3, v3 in v2.items():
                            self.resolve(k3, v3)

    def resolve_ref(v):
        """
        Look for the Ref in the parent template Properties if it matches
        a module Parameter name. If it's not there, use the default if
        there is one. If not, raise an error.

        If there is no matching Parameter, look for a resource with that
        name in this module and fix the logical id so it has the prefix.

        Otherwise just leave it be and assume the module author is
        expecting the parent template to have that Reference.
        """
        if type(v) is not str:
            msg = f"Ref should be a string: {v}"
            raise exceptions.InvalidModuleError(msg=msg)
        # TODO

    def resolve_sub(v):
        pass
        # TODO

    def resolve_getatt(v):
        pass
        # TODO
