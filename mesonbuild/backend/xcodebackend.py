# Copyright 2014-2021 The Meson development team

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from . import backends
from .. import build
from .. import dependencies
from .. import mesonlib
from .. import mlog
import uuid, os, operator
import typing as T

from ..mesonlib import MesonException, OptionKey
from ..interpreter import Interpreter

INDENT = '\t'
XCODETYPEMAP = {'c': 'sourcecode.c.c',
                'a': 'archive.ar',
                'cc': 'sourcecode.cpp.cpp',
                'cxx': 'sourcecode.cpp.cpp',
                'cpp': 'sourcecode.cpp.cpp',
                'c++': 'sourcecode.cpp.cpp',
                'm': 'sourcecode.c.objc',
                'mm': 'sourcecode.cpp.objcpp',
                'h': 'sourcecode.c.h',
                'hpp': 'sourcecode.cpp.h',
                'hxx': 'sourcecode.cpp.h',
                'hh': 'sourcecode.cpp.hh',
                'inc': 'sourcecode.c.h',
                'dylib': 'compiled.mach-o.dylib',
                'o': 'compiled.mach-o.objfile',
                's': 'sourcecode.asm',
                'asm': 'sourcecode.asm',
                }
LANGNAMEMAP = {'c': 'C',
               'cpp': 'CPLUSPLUS',
               'objc': 'OBJC',
               'objcpp': 'OBJCPLUSPLUS',
               }
OPT2XCODEOPT = {'0': '0',
                'g': '0',
                '1': '1',
                '2': '2',
                '3': '3',
                's': 's',
                }
BOOL2XCODEBOOL = {True: 'YES', False: 'NO'}

class PbxItem:
    def __init__(self, value, comment = ''):
        self.value = value
        self.comment = comment

class PbxArray:
    def __init__(self):
        self.items = []

    def add_item(self, item, comment=''):
        if isinstance(item, PbxArrayItem):
            self.items.append(item)
        else:
            self.items.append(PbxArrayItem(item, comment))

    def write(self, ofile, indent_level):
        ofile.write('(\n')
        indent_level += 1
        for i in self.items:
            if i.comment:
                ofile.write(indent_level*INDENT + f'{i.value} {i.comment},\n')
            else:
                ofile.write(indent_level*INDENT + f'{i.value},\n')
        indent_level -= 1
        ofile.write(indent_level*INDENT + ');\n')

class PbxArrayItem:
    def __init__(self, value, comment = ''):
        self.value = value
        if comment:
            if '/*' in comment:
                self.comment = comment
            else:
                self.comment = f'/* {comment} */'
        else:
            self.comment = comment

class PbxComment:
    def __init__(self, text):
        assert(isinstance(text, str))
        assert('/*' not in text)
        self.text = f'/* {text} */'

    def write(self, ofile, indent_level):
        ofile.write(f'\n{self.text}\n')

class PbxDictItem:
    def __init__(self, key, value, comment = ''):
        self.key = key
        self.value = value
        if comment:
            if '/*' in comment:
                self.comment = comment
            else:
                self.comment = f'/* {comment} */'
        else:
            self.comment = comment

class PbxDict:
    def __init__(self):
        # This class is a bit weird, because we want to write PBX dicts in
        # defined order _and_ we want to write intermediate comments also in order.
        self.keys = set()
        self.items = []

    def add_item(self, key, value, comment=''):
        item = PbxDictItem(key, value, comment)
        assert(key not in self.keys)
        self.keys.add(key)
        self.items.append(item)

    def add_comment(self, comment):
        if isinstance(comment, str):
            self.items.append(PbxComment(str))
        else:
            assert(isinstance(comment, PbxComment))
            self.items.append(comment)

    def write(self, ofile, indent_level):
        ofile.write('{\n')
        indent_level += 1
        for i in self.items:
            if isinstance(i, PbxComment):
                i.write(ofile, indent_level)
            elif isinstance(i, PbxDictItem):
                if isinstance(i.value, (str, int)):
                    if i.comment:
                        ofile.write(indent_level*INDENT + f'{i.key} = {i.value} {i.comment};\n')
                    else:
                        ofile.write(indent_level*INDENT + f'{i.key} = {i.value};\n')
                elif isinstance(i.value, PbxDict):
                    if i.comment:
                        ofile.write(indent_level*INDENT + f'{i.key} {i.comment} = ')
                    else:
                        ofile.write(indent_level*INDENT + f'{i.key} = ')
                    i.value.write(ofile, indent_level)
                elif isinstance(i.value, PbxArray):
                    if i.comment:
                        ofile.write(indent_level*INDENT + f'{i.key} {i.comment} = ')
                    else:
                        ofile.write(indent_level*INDENT + f'{i.key} = ')
                    i.value.write(ofile, indent_level)
                else:
                    print(i)
                    print(i.key)
                    print(i.value)
                    raise RuntimeError('missing code')
            else:
                print(i)
                raise RuntimeError('missing code2')

        indent_level -= 1
        ofile.write(indent_level*INDENT + '}')
        if indent_level == 0:
            ofile.write('\n')
        else:
            ofile.write(';\n')

class XCodeBackend(backends.Backend):
    def __init__(self, build: T.Optional[build.Build], interpreter: T.Optional[Interpreter]):
        super().__init__(build, interpreter)
        self.name = 'xcode'
        self.project_uid = self.environment.coredata.lang_guids['default'].replace('-', '')[:24]
        self.buildtype = self.environment.coredata.get_option(OptionKey('buildtype'))
        self.project_conflist = self.gen_id()
        self.maingroup_id = self.gen_id()
        self.all_id = self.gen_id()
        self.all_buildconf_id = self.gen_id()
        self.buildtypes = [self.buildtype]
        self.test_id = self.gen_id()
        self.test_command_id = self.gen_id()
        self.test_buildconf_id = self.gen_id()
        self.top_level_dict = PbxDict()
        # In Xcode files are not accessed via their file names, but rather every one of them
        # gets an unique id. More precisely they get one unique id per target they are used
        # in. If you generate only one id per file and use them, compilation will work but the
        # UI will only show the file in one target but not the others. Thus they key is
        # a tuple containing the target and filename.
        self.buildfile_ids = {}
        # That is not enough, though. Each target/file combination also gets a unique id
        # in the file reference section. Because why not. This means that a source file
        # that is used in two targets gets a total of four unique ID numbers.
        self.fileref_ids = {}

    def write_pbxfile(self, top_level_dict, ofilename):
        with open(ofilename, 'w') as ofile:
            ofile.write('// !$*UTF8*$!\n')
            top_level_dict.write(ofile, 0)

    def gen_id(self):
        return str(uuid.uuid4()).upper().replace('-', '')[:24]

    def get_target_dir(self, target):
        dirname = os.path.join(target.get_subdir(), self.environment.coredata.get_option(OptionKey('buildtype')))
        os.makedirs(os.path.join(self.environment.get_build_dir(), dirname), exist_ok=True)
        return dirname

    def get_custom_target_output_dir(self, target):
        dirname = target.get_subdir()
        os.makedirs(os.path.join(self.environment.get_build_dir(), dirname), exist_ok=True)
        return dirname

    def target_to_build_root(self, target):
        if self.get_target_dir(target) == '':
            return ''
        directories = os.path.normpath(self.get_target_dir(target)).split(os.sep)
        return os.sep.join(['..'] * len(directories))

    def object_filename_from_source(self, target, source):
        # Xcode has the following naming scheme:
        # projectname.build/debug/prog@exe.build/Objects-normal/x86_64/func.o
        project = self.build.project_name
        buildtype = self.buildtype
        tname = target.get_id()
        arch = 'x86_64'
        if isinstance(source, mesonlib.File):
            source = source.fname
        stem = os.path.splitext(os.path.basename(source))[0]
        return f'{project}.build/{buildtype}/{tname}.build/Objects-normal/{arch}/{stem}.o'

    def generate(self):
        self.serialize_tests()
        # Cache the result as the method rebuilds the array every time it is called.
        self.build_targets = self.build.get_build_targets()
        self.custom_targets = self.build.get_custom_targets()
        self.generate_filemap()
        self.generate_buildstylemap()
        self.generate_build_phase_map()
        self.generate_build_configuration_map()
        self.generate_build_configurationlist_map()
        self.generate_project_configurations_map()
        self.generate_buildall_configurations_map()
        self.generate_test_configurations_map()
        self.generate_native_target_map()
        self.generate_native_frameworks_map()
        self.generate_custom_target_map()
        self.generate_source_phase_map()
        self.generate_target_dependency_map()
        self.generate_pbxdep_map()
        self.generate_containerproxy_map()
        self.generate_target_file_maps()
        self.proj_dir = os.path.join(self.environment.get_build_dir(), self.build.project_name + '.xcodeproj')
        os.makedirs(self.proj_dir, exist_ok=True)
        self.proj_file = os.path.join(self.proj_dir, 'project.pbxproj')
        objects_dict = self.generate_prefix(self.top_level_dict)
        objects_dict.add_comment(PbxComment('Begin PBXAggregateTarget section'))
        self.generate_pbx_aggregate_target(objects_dict)
        objects_dict.add_comment(PbxComment('End PBXAggregateTarget section'))
        objects_dict.add_comment(PbxComment('Begin PBXBuildFile section'))
        self.generate_pbx_build_file(objects_dict)
        objects_dict.add_comment(PbxComment('End PBXBuildFile section'))
        objects_dict.add_comment(PbxComment('Begin PBXBuildStyle section'))
        self.generate_pbx_build_style(objects_dict)
        objects_dict.add_comment(PbxComment('End PBXBuildStyle section'))
        objects_dict.add_comment(PbxComment('Begin PBXContainerItemProxy section'))
        self.generate_pbx_container_item_proxy(objects_dict)
        objects_dict.add_comment(PbxComment('End PBXContainerItemProxy section'))
        objects_dict.add_comment(PbxComment('Begin PBXFileReference section'))
        self.generate_pbx_file_reference(objects_dict)
        objects_dict.add_comment(PbxComment('End PBXFileReference section'))
        objects_dict.add_comment(PbxComment('Begin PBXFrameworksBuildPhase section'))
        self.generate_pbx_frameworks_buildphase(objects_dict)
        objects_dict.add_comment(PbxComment('End PBXFrameworksBuildPhase section'))
        objects_dict.add_comment(PbxComment('Begin PBXGroup section'))
        self.generate_pbx_group(objects_dict)
        objects_dict.add_comment(PbxComment('End PBXGroup section'))
        objects_dict.add_comment(PbxComment('Begin PBXNativeTarget section'))
        self.generate_pbx_native_target(objects_dict)
        objects_dict.add_comment(PbxComment('End PBXNativeTarget section'))
        objects_dict.add_comment(PbxComment('Begin PBXProject section'))
        self.generate_pbx_project(objects_dict)
        objects_dict.add_comment(PbxComment('End PBXProject section'))
        objects_dict.add_comment(PbxComment('Begin PBXShellScriptBuildPhase section'))
        self.generate_pbx_shell_build_phase(objects_dict)
        objects_dict.add_comment(PbxComment('End PBXShellScriptBuildPhase section'))
        objects_dict.add_comment(PbxComment('Begin PBXSourcesBuildPhase section'))
        self.generate_pbx_sources_build_phase(objects_dict)
        objects_dict.add_comment(PbxComment('End PBXSourcesBuildPhase section'))
        objects_dict.add_comment(PbxComment('Begin PBXTargetDependency section'))
        self.generate_pbx_target_dependency(objects_dict)
        objects_dict.add_comment(PbxComment('End PBXTargetDependency section'))
        objects_dict.add_comment(PbxComment('Begin XCBuildConfiguration section'))
        self.generate_xc_build_configuration(objects_dict)
        objects_dict.add_comment(PbxComment('End XCBuildConfiguration section'))
        objects_dict.add_comment(PbxComment('Begin XCConfigurationList section'))
        self.generate_xc_configurationList(objects_dict)
        objects_dict.add_comment(PbxComment('End XCConfigurationList section'))
        self.generate_suffix(self.top_level_dict)
        self.write_pbxfile(self.top_level_dict, self.proj_file)

    def get_xcodetype(self, fname):
        xcodetype = XCODETYPEMAP.get(fname.split('.')[-1].lower())
        if not xcodetype:
            xcodetype = 'sourcecode.unknown'
            mlog.warning(f'Unknown file type "{fname}" fallbacking to "{xcodetype}". Xcode project might be malformed.')
        return xcodetype

    def generate_filemap(self):
        self.filemap = {} # Key is source file relative to src root.
        self.target_filemap = {}
        for name, t in self.build_targets.items():
            for s in t.sources:
                if isinstance(s, mesonlib.File):
                    s = os.path.join(s.subdir, s.fname)
                    self.filemap[s] = self.gen_id()
            for o in t.objects:
                if isinstance(o, str):
                    o = os.path.join(t.subdir, o)
                    self.filemap[o] = self.gen_id()
            self.target_filemap[name] = self.gen_id()

    def generate_buildstylemap(self):
        self.buildstylemap = {self.buildtype: self.gen_id()}

    def generate_build_phase_map(self):
        for tname, t in self.build_targets.items():
            # generate id for our own target-name
            t.buildphasemap = {}
            t.buildphasemap[tname] = self.gen_id()
            # each target can have it's own Frameworks/Sources/..., generate id's for those
            t.buildphasemap['Frameworks'] = self.gen_id()
            t.buildphasemap['Resources'] = self.gen_id()
            t.buildphasemap['Sources'] = self.gen_id()

    def generate_build_configuration_map(self):
        self.buildconfmap = {}
        for t in self.build_targets:
            bconfs = {self.buildtype: self.gen_id()}
            self.buildconfmap[t] = bconfs

    def generate_project_configurations_map(self):
        self.project_configurations = {self.buildtype: self.gen_id()}

    def generate_buildall_configurations_map(self):
        self.buildall_configurations = {self.buildtype: self.gen_id()}

    def generate_test_configurations_map(self):
        self.test_configurations = {self.buildtype: self.gen_id()}

    def generate_build_configurationlist_map(self):
        self.buildconflistmap = {}
        for t in self.build_targets:
            self.buildconflistmap[t] = self.gen_id()

    def generate_native_target_map(self):
        self.native_targets = {}
        for t in self.build_targets:
            self.native_targets[t] = self.gen_id()

    def generate_custom_target_map(self):
        self.shell_targets = {}
        self.custom_target_output_buildfile = {}
        self.custom_target_output_fileref = {}
        for tname, t in self.custom_targets.items():
            self.shell_targets[tname] = self.gen_id()
            if not isinstance(t, build.CustomTarget):
                continue
            (srcs, ofilenames, cmd) = self.eval_custom_target_command(t)
            for o in ofilenames:
                self.custom_target_output_buildfile[o] = self.gen_id()
                self.custom_target_output_fileref[o] = self.gen_id()

    def generate_native_frameworks_map(self):
        self.native_frameworks = {}
        self.native_frameworks_fileref = {}
        for t in self.build_targets.values():
            for dep in t.get_external_deps():
                if isinstance(dep, dependencies.AppleFrameworks):
                    for f in dep.frameworks:
                        self.native_frameworks[f] = self.gen_id()
                        self.native_frameworks_fileref[f] = self.gen_id()

    def generate_target_dependency_map(self):
        self.target_dependency_map = {}
        for tname, t in self.build_targets.items():
            for target in t.link_targets:
                self.target_dependency_map[(tname, target.get_basename())] = self.gen_id()

    def generate_pbxdep_map(self):
        self.pbx_dep_map = {}
        for t in self.build_targets:
            self.pbx_dep_map[t] = self.gen_id()

    def generate_containerproxy_map(self):
        self.containerproxy_map = {}
        for t in self.build_targets:
            self.containerproxy_map[t] = self.gen_id()

    def generate_target_file_maps(self):
        for tname, t in self.build_targets.items():
            for s in t.sources:
                if isinstance(s, mesonlib.File):
                    s = os.path.join(s.subdir, s.fname)
                if not isinstance(s, str):
                    continue
                self.buildfile_ids[(tname, s)] = self.gen_id()
                self.fileref_ids[(tname, s)] = self.gen_id()
            for o in t.objects:
                if isinstance(o, build.ExtractedObjects):
                    # Extracted objects do not live in "the Xcode world".
                    continue
                else:
                    o = os.path.join(t.subdir, o)
                    self.buildfile_ids[(tname, o)] = self.gen_id()
                    self.fileref_ids[(tname, o)] = self.gen_id()

    def generate_source_phase_map(self):
        self.source_phase = {}
        for t in self.build_targets:
            self.source_phase[t] = self.gen_id()

    def generate_pbx_aggregate_target(self, objects_dict):
        target_dependencies = list(map(lambda t: self.pbx_dep_map[t], self.build_targets))
        aggregated_targets = []
        aggregated_targets.append((self.all_id, 'ALL_BUILD', self.all_buildconf_id, [], target_dependencies))
        aggregated_targets.append((self.test_id, 'RUN_TESTS', self.test_buildconf_id, [self.test_command_id], []))
        # Sort objects by ID before writing
        sorted_aggregated_targets = sorted(aggregated_targets, key=operator.itemgetter(0))
        for t in sorted_aggregated_targets:
            agt_dict = PbxDict()
            name = t[1]
            buildconf_id = t[2]
            build_phases = t[3]
            dependencies = t[4]
            agt_dict.add_item('isa', 'PBXAggregateTarget')
            agt_dict.add_item('buildConfigurationList', buildconf_id, f'Build configuration list for PBXAggregateTarget "{name}"')
            bp_arr = PbxArray()
            agt_dict.add_item('buildPhases', bp_arr)
            for bp in build_phases:
                bp_arr.add_item(bp, 'ShellScript')
            dep_arr = PbxArray()
            agt_dict.add_item('dependencies', dep_arr)
            for td in dependencies:
                dep_arr.add_item(td, 'PBXTargetDependency')
            agt_dict.add_item('name', name)
            agt_dict.add_item('productName', name)
            objects_dict.add_item(t[0], agt_dict, name)

    def generate_pbx_build_file(self, objects_dict):
        for tname, t in self.build_targets.items():
            for dep in t.get_external_deps():
                if isinstance(dep, dependencies.AppleFrameworks):
                    for f in dep.frameworks:
                        fw_dict = PbxDict()
                        objects_dict.add:item(self.native_frameworks[f], fw_dict, f'{f}.framework in Frameworks')
                        fw_dict.add_item('isa', 'PBXBuildFile')
                        fw_dict.add_item('fileRef', self.native_frameworks_fileref[f], f)

            for s in t.sources:
                in_build_dir = False
                if isinstance(s, mesonlib.File):
                    if s.is_built:
                        in_build_dir = True
                    s = os.path.join(s.subdir, s.fname)
                    
                if not isinstance(s, str):
                    continue
                sdict = PbxDict()
                idval = self.buildfile_ids[(tname, s)]
                fileref = self.fileref_ids[(tname, s)]
                if in_build_dir:
                    fullpath = os.path.join(self.environment.get_build_dir(), s)    
                else:
                    fullpath = os.path.join(self.environment.get_source_dir(), s)
                compiler_args = ''
                sdict.add_item('isa', 'PBXBuildFile')
                sdict.add_item('fileRef', fileref, fullpath)
                objects_dict.add_item(idval, sdict)

            for o in t.objects:
                if isinstance(o, build.ExtractedObjects):
                    # Object files are not source files as such. We add them
                    # by hand in linker flags. It is also not particularly
                    # clear how to define build files in Xcode's file format.
                    continue
                o = os.path.join(t.subdir, o)
                idval = self.buildfile_ids[(tname, o)]
                fileref = self.fileref_ids[(tname, s)]
                self.targetfile_ids[(tname, s)] = idval
                fullpath = os.path.join(self.environment.get_source_dir(), o)
                fullpath2 = fullpath
                o_dict = PbxDict()
                objects_dict.add_item(idval, o_dict, fullpath)
                o_dict.add_item('isa', 'PBXBuildFile')
                o_dict.add_item('fileRef', fileref, fullpath2)

        # Custom targets are shell build phases in Xcode terminology.
        for tname, t in self.custom_targets.items():
            if not isinstance(t, build.CustomTarget):
                continue
            (srcs, ofilenames, cmd) = self.eval_custom_target_command(t)
            for o in ofilenames:
                custom_dict = PbxDict()
                objects_dict.add_item(self.custom_target_output_buildfile[o], custom_dict, f'/* {o} */')
                custom_dict.add_item('isa', 'PBXBuildFile')
                custom_dict.add_item('fileRef', self.custom_target_output_fileref[o])

    def generate_pbx_build_style(self, objects_dict):
        # FIXME: Xcode 9 and later does not uses PBXBuildStyle and it gets removed. Maybe we can remove this part.
        for name, idval in self.buildstylemap.items():
            styledict = PbxDict()
            objects_dict.add_item(idval, styledict, name)
            styledict.add_item('isa', 'PBXBuildStyle')
            settings_dict = PbxDict()
            styledict.add_item('buildSettings', settings_dict)
            settings_dict.add_item('COPY_PHASE_STRIP', 'NO')
            styledict.add_item('name', f'"{name}"')

    def generate_pbx_container_item_proxy(self, objects_dict):
        for t in self.build_targets:
            proxy_dict = PbxDict()
            objects_dict.add_item(self.containerproxy_map[t], proxy_dict, 'PBXContainerItemProxy')
            proxy_dict.add_item('isa', 'PBXContainerItemProxy')
            proxy_dict.add_item('containerPortal', self.project_uid, 'Project object')
            proxy_dict.add_item('proxyType', '1')
            proxy_dict.add_item('remoteGlobalIDString', self.native_targets[t])
            proxy_dict.add_item('remoteInfo', '"' + t + '"')

    def generate_pbx_file_reference(self, objects_dict):
        for tname, t in self.build_targets.items():
            for dep in t.get_external_deps():
                if isinstance(dep, dependencies.AppleFrameworks):
                    for f in dep.frameworks:
                        fw_dict = PbxDict()
                        objects_dict.add_item(self.native_frameworks_fileref[f], fw_dict, f)
                        fw_dict.add_item('isa', 'PBXFileReference')
                        fw_dict.add_item('lastKnownFileType', 'wrapper.framework')
                        fw_dict.add_item('name', f'{f}.framework')
                        fw_dict.add_item('path', f'System/Library/Frameworks/{f}.framework')
                        fw_dict.add_item('sourceTree', 'SDKROOT')
            for s in t.sources:
                in_build_dir = False
                if isinstance(s, mesonlib.File):
                    if s.is_built:
                        in_build_dir = True
                    s = os.path.join(s.subdir, s.fname)
                if not isinstance(s, str):
                    continue
                idval = self.fileref_ids[(tname, s)]
                fullpath = os.path.join(self.environment.get_source_dir(), s)
                src_dict = PbxDict()
                xcodetype = self.get_xcodetype(s)
                name = os.path.basename(s)
                path = s
                objects_dict.add_item(idval, src_dict, fullpath)
                src_dict.add_item('isa', 'PBXFileReference')
                src_dict.add_item('explicitFileType', '"' + xcodetype + '"')
                src_dict.add_item('fileEncoding', '4')
                if in_build_dir:
                    src_dict.add_item('name', '"' + name + '"')
                    # This makes no sense. This should say path instead of name
                    # but then the path gets added twice.
                    src_dict.add_item('path', '"' + name + '"')
                    src_dict.add_item('sourceTree', 'BUILD_ROOT')
                else:
                    src_dict.add_item('name', '"' + name + '"')
                    src_dict.add_item('path', '"' + path + '"')
                    src_dict.add_item('sourceTree', 'SOURCE_ROOT')


            for o in t.objects:
                if isinstance(o, build.ExtractedObjects):
                    # Same as with pbxbuildfile.
                    continue
                o = os.path.join(t.subdir, o)
                idval = self.fileref_ids[(tname, o)]
                fileref = self.filemap[o]
                fullpath = os.path.join(self.environment.get_source_dir(), o)
                fullpath2 = fullpath
                o_dict = PbxDict()
                objects_dict.add_item(idval, o_dict, fullpath)
                o_dict.add_item('isa', 'PBXBuildFile')
                o_dict.add_item('fileRef', fileref, fullpath2)
        for tname, idval in self.target_filemap.items():
            target_dict = PbxDict()
            objects_dict.add_item(idval, target_dict, tname)
            t = self.build_targets[tname]
            fname = t.get_filename()
            reftype = 0
            if isinstance(t, build.Executable):
                typestr = 'compiled.mach-o.executable'
                path = fname
            elif isinstance(t, build.SharedLibrary):
                typestr = self.get_xcodetype('dummy.dylib')
                path = fname
            else:
                typestr = self.get_xcodetype(fname)
                path = '"%s"' % t.get_filename()
            target_dict.add_item('isa', 'PBXFileReference')
            target_dict.add_item('explicitFileType', '"' + typestr + '"')
            target_dict.add_item('path', path)
            target_dict.add_item('refType', reftype)
            target_dict.add_item('sourceTree', 'BUILT_PRODUCTS_DIR')

        for tname, t in self.custom_targets.items():
            if not isinstance(t, build.CustomTarget):
                continue
            (srcs, ofilenames, cmd) = self.eval_custom_target_command(t)
            for o in ofilenames:
                custom_dict = PbxDict()
                typestr = self.get_xcodetype(o)
                custom_dict.add_item('isa', 'PBXFileReference')
                custom_dict.add_item('explicitFileType', '"' + typestr + '"')
                custom_dict.add_item('name', o)
                custom_dict.add_item('path', os.path.join(self.src_to_build, o))
                custom_dict.add_item('refType', 0)
                custom_dict.add_item('sourceTree', 'SOURCE_ROOT')
                objects_dict.add_item(self.custom_target_output_fileref[o], custom_dict)

    def generate_pbx_frameworks_buildphase(self, objects_dict):
        for t in self.build_targets.values():
            bt_dict = PbxDict()
            objects_dict.add_item(t.buildphasemap['Frameworks'], bt_dict, 'Frameworks')
            bt_dict.add_item('isa', 'PBXFrameworksBuildPhase')
            bt_dict.add_item('buildActionMask', 2147483647)
            file_list = PbxArray()
            bt_dict.add_item('files', file_list)
            for dep in t.get_external_deps():
                if isinstance(dep, dependencies.AppleFrameworks):
                    for f in dep.frameworks:
                        file_list.add_item(self.native_frameworks[f], f'{f}.framework in Frameworks')
            bt_dict.add_item('runOnlyForDeploymentPostprocessing', 0)

    def generate_pbx_group(self, objects_dict):
        groupmap = {}
        target_src_map = {}
        for t in self.build_targets:
            groupmap[t] = self.gen_id()
            target_src_map[t] = self.gen_id()
        sources_id = self.gen_id()
        resources_id = self.gen_id()
        products_id = self.gen_id()
        frameworks_id = self.gen_id()        
        main_dict = PbxDict()
        objects_dict.add_item(self.maingroup_id, main_dict)
        main_dict.add_item('isa', 'PBXGroup')
        main_children = PbxArray()
        main_dict.add_item('children', main_children)
        main_children.add_item(sources_id, 'Sources')
        main_children.add_item(resources_id, 'Resources')
        main_children.add_item(products_id, 'Products')
        main_children.add_item(frameworks_id, 'Frameworks')
        main_dict.add_item('sourceTree', '"<group>"')

        # Sources
        source_dict = PbxDict()
        objects_dict.add_item(sources_id, source_dict, 'Sources')
        source_dict.add_item('isa', 'PBXGroup')
        source_children = PbxArray()
        source_dict.add_item('children', source_children)
        for t in self.build_targets:
            source_children.add_item(groupmap[t], t)
        source_dict.add_item('name', 'Sources')
        source_dict.add_item('sourceTree', '"<group>"')

        resource_dict = PbxDict()
        objects_dict.add_item(resources_id, resource_dict, 'Resources')
        resource_dict.add_item('isa', 'PBXGroup')
        resource_children = PbxArray()
        resource_dict.add_item('children', resource_children)
        resource_dict.add_item('name', 'Resources')
        resource_dict.add_item('sourceTree', '"<group>"')

        frameworks_dict = PbxDict()
        objects_dict.add_item(frameworks_id, frameworks_dict, 'Frameworks')
        frameworks_dict.add_item('isa', 'PBXGroup')
        frameworks_children = PbxArray()
        frameworks_dict.add_item('children', frameworks_children)
        # write frameworks

        for t in self.build_targets.values():
            for dep in t.get_external_deps():
                if isinstance(dep, dependencies.AppleFrameworks):
                    for f in dep.frameworks:
                        frameworks_children.add_item(self.native_frameworks_fileref[f], f)

        frameworks_dict.add_item('name', 'Frameworks')
        frameworks_dict.add_item('sourceTree', '"<group>"')

        # Targets
        for tname, t in self.build_targets.items():
            target_dict = PbxDict()
            objects_dict.add_item(groupmap[tname], target_dict, tname)
            target_dict.add_item('isa', 'PBXGroup')
            target_children = PbxArray()
            target_dict.add_item('children', target_children)
            target_children.add_item(target_src_map[tname], 'Source files')
            target_dict.add_item('name', f'"{t}"')
            target_dict.add_item('sourceTree', '"<group>"')
            source_files_dict = PbxDict()
            objects_dict.add_item(target_src_map[tname], source_files_dict, 'Source files')
            source_files_dict.add_item('isa', 'PBXGroup')
            source_file_children = PbxArray()
            source_files_dict.add_item('children', source_file_children)
            for s in t.sources:
                if isinstance(s, mesonlib.File):
                    s = os.path.join(t.subdir, s.fname)
                if not isinstance(s, str):
                    clontinue                
                source_file_children.add_item(self.fileref_ids[(tname, s)], s)
            for o in t.objects:
                if isinstance(o, build.ExtractedObjects):
                    # Do not show built object files in the project tree.   
                    continue
                o = os.path.join(t.subdir, o)
                source_file_children.add_item(self.fileref_ids[(tname, o)], o)
            source_files_dict.add_item('name', '"Source files"')
            source_files_dict.add_item('sourceTree', '"<group>"')

        # And finally products
        product_dict = PbxDict()
        objects_dict.add_item(products_id, product_dict, 'Products')
        product_dict.add_item('isa', 'PBXGroup')
        product_children = PbxArray()
        product_dict.add_item('children', product_children)
        for t in self.build_targets:
            product_children.add_item(self.target_filemap[t], t)
        product_dict.add_item('name', 'Products')
        product_dict.add_item('sourceTree', '"<group>"')

    def generate_pbx_native_target(self, objects_dict):
        for tname, idval in self.native_targets.items():
            ntarget_dict = PbxDict()
            t = self.build_targets[tname]
            objects_dict.add_item(idval, ntarget_dict, tname)
            ntarget_dict.add_item('isa', 'PBXNativeTarget')
            ntarget_dict.add_item('buildConfigurationList', self.buildconflistmap[tname], f'Build configuration list for PBXNativeTarget "{tname}"')
            buildphases_array = PbxArray()
            ntarget_dict.add_item('buildPhases', buildphases_array)
            for g in t.generated:
                if isinstance(g, build.CustomTarget):
                    buildphases_array.add_item(self.shell_targets[g.get_id()], f'/* {g.name} */')
            for bpname, bpval in t.buildphasemap.items():
                buildphases_array.add_item(bpval, f'{bpname} yyy')
            ntarget_dict.add_item('buildRules', PbxArray())
            dep_array = PbxArray()
            ntarget_dict.add_item('dependencies', dep_array)
            for lt in self.build_targets[tname].link_targets:
                # NOT DOCUMENTED, may need to make different links
                # to same target have different targetdependency item.
                idval = self.pbx_dep_map[lt.get_id()]
                dep_array.add_item(idval, 'PBXTargetDependency')
            for o in t.objects:
                if isinstance(o, build.ExtractedObjects):
                    source_target_id = o.target.get_id()
                    idval = self.pbx_dep_map[source_target_id]
                    dep_array.add_item(idval, 'PBXTargetDependency')

            ntarget_dict.add_item('name', f'"{tname}"')
            ntarget_dict.add_item('productName', f'"{tname}"')
            ntarget_dict.add_item('productReference', self.target_filemap[tname], tname)
            if isinstance(t, build.Executable):
                typestr = 'com.apple.product-type.tool'
            elif isinstance(t, build.StaticLibrary):
                typestr = 'com.apple.product-type.library.static'
            elif isinstance(t, build.SharedLibrary):
                typestr = 'com.apple.product-type.library.dynamic'
            else:
                raise MesonException('Unknown target type for %s' % tname)
            ntarget_dict.add_item('productType', f'"{typestr}"')

    def generate_pbx_project(self, objects_dict):
        project_dict = PbxDict()
        objects_dict.add_item(self.project_uid, project_dict, 'Project object')
        project_dict.add_item('isa', 'PBXProject')
        attr_dict = PbxDict()
        project_dict.add_item('attributes', attr_dict)
        attr_dict.add_item('BuildIndependentTargetsInParallel', 'YES')
        project_dict.add_item('buildConfigurationList', self.project_conflist, f'Build configuration list for PBXProject "{self.build.project_name}"')
        project_dict.add_item('buildSettings', PbxDict())
        style_arr = PbxArray()
        project_dict.add_item('buildStyles', style_arr)
        for name, idval in self.buildstylemap.items():
            style_arr.add_item(idval, name)
        project_dict.add_item('compatibilityVersion', '"Xcode 3.2"')
        project_dict.add_item('hasScannedForEncodings', 0)
        project_dict.add_item('mainGroup', self.maingroup_id)
        project_dict.add_item('projectDirPath', f'"{self.build_to_src}"')
        project_dict.add_item('projectRoot', '""')
        targets_arr = PbxArray()
        project_dict.add_item('targets', targets_arr)
        targets_arr.add_item(self.all_id, 'ALL_BUILD')
        targets_arr.add_item(self.test_id, 'RUN_TESTS')
        for t in self.build_targets:
            targets_arr.add_item(self.native_targets[t], t)

    def generate_pbx_shell_build_phase(self, objects_dict):
        shell_dict = PbxDict()
        objects_dict.add_item(self.test_command_id, shell_dict, 'ShellScript')
        shell_dict.add_item('isa', 'PBXShellScriptBuildPhase')
        shell_dict.add_item('buildActionMask', 2147483647)
        shell_dict.add_item('files', PbxArray())
        shell_dict.add_item('inputPaths', PbxArray())
        shell_dict.add_item('outputPaths', PbxArray())
        shell_dict.add_item('runOnlyForDeploymentPostprocessing', 0)
        shell_dict.add_item('shellPath', '/bin/sh')
        cmd = mesonlib.get_meson_command() + ['test', '--no-rebuild', '-C', self.environment.get_build_dir()]
        cmdstr = ' '.join(["'%s'" % i for i in cmd])
        shell_dict.add_item('shellScript', f'"{cmdstr}"')
        shell_dict.add_item('showEnvVarsInLog', 0)
        # Custom targets are shell build phases in Xcode terminology.
        for tname, t in self.custom_targets.items():
            if not isinstance(t, build.CustomTarget):
                continue
            (srcs, ofilenames, cmd) = self.eval_custom_target_command(t)
            custom_dict = PbxDict()
            objects_dict.add_item(self.shell_targets[tname], custom_dict, f'/* Custom target {tname} */')
            custom_dict.add_item('isa', 'PBXShellScriptBuildPhase')
            custom_dict.add_item('buildActionMask', 2147483647)
            custom_dict.add_item('files', PbxArray())
            custom_dict.add_item('inputPaths', PbxArray())
            outarray = PbxArray()
            custom_dict.add_item('name', '"Generate {}."'.format(ofilenames[0]))
            custom_dict.add_item('outputPaths', outarray)
            for o in ofilenames:
                outarray.add_item(os.path.join(self.environment.get_build_dir(), o))
            custom_dict.add_item('runOnlyForDeploymentPostprocessing', 0)
            custom_dict.add_item('shellPath', '/bin/sh')
            workdir = self.environment.get_build_dir()
            cmdstr = ' '.join([f'\\"{x}\\"' for x in cmd])
            custom_dict.add_item('shellScript', f'"cd {workdir}; {cmdstr}"')
            custom_dict.add_item('showEnvVarsInLog', 0)

    def generate_pbx_sources_build_phase(self, objects_dict):
        for name in self.source_phase.keys():
            phase_dict = PbxDict()
            t = self.build_targets[name]
            objects_dict.add_item(t.buildphasemap[name], phase_dict, 'Sources')
            phase_dict.add_item('isa', 'PBXSourcesBuildPhase')
            phase_dict.add_item('buildActionMask', 2147483647)
            file_arr = PbxArray()
            phase_dict.add_item('files', file_arr)
            for s in self.build_targets[name].sources:
                s = os.path.join(s.subdir, s.fname)
                if not self.environment.is_header(s):
                    file_arr.add_item(self.buildfile_ids[(name, s)], os.path.join(self.environment.get_source_dir(), s))
            for tname, t in self.custom_targets.items():
                (srcs, ofilenames, cmd) = self.eval_custom_target_command(t)
                for o in ofilenames:
                    file_arr.add_item(self.custom_target_output_buildfile[o],
                                      os.path.join(self.environment.get_build_dir(), o))
            phase_dict.add_item('runOnlyForDeploymentPostprocessing', 0)

    def generate_pbx_target_dependency(self, objects_dict):
        targets = []
        for t in self.build_targets:
            idval = self.pbx_dep_map[t] # VERIFY: is this correct?
            targets.append((idval, self.native_targets[t], t, self.containerproxy_map[t]))

        # Sort object by ID
        sorted_targets = sorted(targets, key=operator.itemgetter(0))
        for t in sorted_targets:
            t_dict = PbxDict()
            objects_dict.add_item(t[0], t_dict, 'PBXTargetDependency')
            t_dict.add_item('isa', 'PBXTargetDependency')
            t_dict.add_item('target', t[1], t[2])
            t_dict.add_item('targetProxy', t[3], 'PBXContainerItemProxy')

    def generate_xc_build_configuration(self, objects_dict):
        # First the setup for the toplevel project.
        for buildtype in self.buildtypes:
            bt_dict = PbxDict()
            objects_dict.add_item(self.project_configurations[buildtype], bt_dict, buildtype)
            bt_dict.add_item('isa', 'XCBuildConfiguration')
            settings_dict = PbxDict()
            bt_dict.add_item('buildSettings', settings_dict)
            settings_dict.add_item('ARCHS', '"$(NATIVE_ARCH_ACTUAL)"')
            settings_dict.add_item('ONLY_ACTIVE_ARCH', 'YES')
            settings_dict.add_item('SDKROOT', '"macosx"')
            settings_dict.add_item('SYMROOT', '"%s/build"' % self.environment.get_build_dir())
            bt_dict.add_item('name', f'"{buildtype}"')

        # Then the all target.
        for buildtype in self.buildtypes:
            bt_dict = PbxDict()
            objects_dict.add_item(self.buildall_configurations[buildtype], bt_dict, buildtype)
            bt_dict.add_item('isa', 'XCBuildConfiguration')
            settings_dict = PbxDict()
            bt_dict.add_item('buildSettings', settings_dict)
            settings_dict.add_item('COMBINE_HIDPI_IMAGES', 'YES')
            settings_dict.add_item('GCC_INLINES_ARE_PRIVATE_EXTERN', 'NO')
            settings_dict.add_item('GCC_PREPROCESSOR_DEFINITIONS', '""')
            settings_dict.add_item('GCC_SYMBOLS_PRIVATE_EXTERN', 'NO')
            settings_dict.add_item('INSTALL_PATH', '""')
            settings_dict.add_item('OTHER_CFLAGS', '""')
            settings_dict.add_item('OTHER_LDFLAGS', '""')
            settings_dict.add_item('OTHER_REZFLAGS', '""')
            settings_dict.add_item('PRODUCT_NAME', 'ALL_BUILD')
            settings_dict.add_item('SECTORDER_FLAGS', '""')
            settings_dict.add_item('SYMROOT', '"%s"' % self.environment.get_build_dir())
            settings_dict.add_item('USE_HEADERMAP', 'NO')
            warn_array = PbxArray()
            warn_array.add_item('"$(inherited)"')
            settings_dict.add_item('WARNING_CFLAGS', warn_array)
        
            bt_dict.add_item('name', f'"{buildtype}"')

        # Then the test target.
        for buildtype in self.buildtypes:
            bt_dict = PbxDict()
            objects_dict.add_item(self.test_configurations[buildtype], bt_dict, buildtype)
            bt_dict.add_item('isa', 'XCBuildConfiguration')
            settings_dict = PbxDict()
            bt_dict.add_item('buildSettings', settings_dict)
            settings_dict.add_item('COMBINE_HIDPI_IMAGES', 'YES')
            settings_dict.add_item('GCC_INLINES_ARE_PRIVATE_EXTERN', 'NO')
            settings_dict.add_item('GCC_PREPROCESSOR_DEFINITIONS', '""')
            settings_dict.add_item('GCC_SYMBOLS_PRIVATE_EXTERN', 'NO')
            settings_dict.add_item('INSTALL_PATH', '""')
            settings_dict.add_item('OTHER_CFLAGS', '""')
            settings_dict.add_item('OTHER_LDFLAGS', '""')
            settings_dict.add_item('OTHER_REZFLAGS', '""')
            settings_dict.add_item('PRODUCT_NAME', 'RUN_TESTS')
            settings_dict.add_item('SECTORDER_FLAGS', '""')
            settings_dict.add_item('SYMROOT', '"%s"' % self.environment.get_build_dir())
            settings_dict.add_item('USE_HEADERMAP', 'NO')
            warn_array = PbxArray()
            settings_dict.add_item('WARNING_CFLAGS', warn_array)
            warn_array.add_item('"$(inherited)"')
            bt_dict.add_item('name', f'"{buildtype}"')

        # Now finally targets.
        for target_name, target in self.build_targets.items():
            self.generate_single_build_target(objects_dict, target_name, target)

        
    def generate_single_build_target(self, objects_dict, target_name, target):
        for buildtype in self.buildtypes:
            dep_libs = []
            links_dylib = False
            headerdirs = []
            for d in target.include_dirs:
                for sd in d.incdirs:
                    cd = os.path.join(d.curdir, sd)
                    headerdirs.append(os.path.join(self.environment.get_source_dir(), cd))
                    headerdirs.append(os.path.join(self.environment.get_build_dir(), cd))
            for l in target.link_targets:
                abs_path = os.path.join(self.environment.get_build_dir(),
                                        l.subdir, buildtype, l.get_filename())
                dep_libs.append("'%s'" % abs_path)
                if isinstance(l, build.SharedLibrary):
                    links_dylib = True
            if links_dylib:
                dep_libs = ['-Wl,-search_paths_first', '-Wl,-headerpad_max_install_names'] + dep_libs
            dylib_version = None
            if isinstance(target, build.SharedLibrary):
                ldargs = ['-dynamiclib', '-Wl,-headerpad_max_install_names'] + dep_libs
                install_path = os.path.join(self.environment.get_build_dir(), target.subdir, buildtype)
                dylib_version = target.soversion
            else:
                ldargs = dep_libs
                install_path = ''
            if dylib_version is not None:
                product_name = target.get_basename() + '.' + dylib_version
            else:
                product_name = target.get_basename()
            ldargs += target.link_args
            linker, stdlib_args = self.determine_linker_and_stdlib_args(target)
            ldargs += self.build.get_project_link_args(linker, target.subproject, target.for_machine)
            if not isinstance(target, build.StaticLibrary):
                ldargs += self.build.get_global_link_args(linker, target.for_machine)
            cargs = []
            for dep in target.get_external_deps():
                cargs += dep.get_compile_args()
                ldargs += dep.get_link_args()
            for o in target.objects:
                # Add extracted objects to the link line by hand.
                if isinstance(o, build.ExtractedObjects):
                    added_objs = set()
                    for objname_rel in o.get_outputs(self):
                        objname_abs = os.path.join(self.environment.get_build_dir(), objname_rel)
                        if objname_abs not in added_objs:
                            added_objs.add(objname_abs)
                            ldargs += [r'\"' + objname_abs + r'\"']
            ldstr = ' '.join(ldargs)
            valid = self.buildconfmap[target_name][buildtype]
            langargs = {}
            for lang in self.environment.coredata.compilers[target.for_machine]:
                if lang not in LANGNAMEMAP:
                    continue
                compiler = target.compilers.get(lang)
                if compiler is None:
                    continue
                # Start with warning args
                warn_args = compiler.get_warn_args(self.get_option_for_target(OptionKey('warning_level'), target))
                copt_proxy = self.get_compiler_options_for_target(target)
                std_args = compiler.get_option_compile_args(copt_proxy)
                # Add compile args added using add_project_arguments()
                pargs = self.build.projects_args[target.for_machine].get(target.subproject, {}).get(lang, [])
                # Add compile args added using add_global_arguments()
                # These override per-project arguments
                gargs = self.build.global_args[target.for_machine].get(lang, [])
                targs = target.get_extra_args(lang)
                args = warn_args + std_args + pargs + gargs + targs
                if args:
                    langname = LANGNAMEMAP[lang]
                    lang_cargs = cargs
                    if compiler and target.implicit_include_directories:
                        # It is unclear what is the cwd when xcode runs. -I. does not seem to
                        # add the root build dir to the search path. So add an absolute path instead.
                        # This may break reproducible builds, in which case patches are welcome.
                        lang_cargs += self.get_build_dir_include_args(target, compiler, absolute_path=True)
                    langargs[langname] = args
                    langargs[langname] += lang_cargs
            symroot = os.path.join(self.environment.get_build_dir(), target.subdir)
            bt_dict = PbxDict()
            objects_dict.add_item(valid, bt_dict, buildtype)
            bt_dict.add_item('isa', 'XCBuildConfiguration')
            settings_dict = PbxDict()
            bt_dict.add_item('buildSettings', settings_dict)
            settings_dict.add_item('COMBINE_HIDPI_IMAGES', 'YES')
            if dylib_version is not None:
                settings_dict.add_item('DYLIB_CURRENT_VERSION', f'"{dylib_version}')
            if target.prefix:
                settings_dict.add_item('EXECUTABLE_PREFIX', target.prefix)
            if target.suffix:
                suffix = '.' + target.suffix
                settings_dict.add_item('EXECUTABLE_SUFFIX', suffix)
            settings_dict.add_item('GCC_GENERATE_DEBUGGING_SYMBOLS', BOOL2XCODEBOOL[self.get_option_for_target(OptionKey('debug'), target)])
            settings_dict.add_item('GCC_INLINES_ARE_PRIVATE_EXTERN', 'NO')
            settings_dict.add_item('GCC_OPTIMIZATION_LEVEL', OPT2XCODEOPT[self.get_option_for_target(OptionKey('optimization'), target)])
            if target.has_pch:
                # Xcode uses GCC_PREFIX_HEADER which only allows one file per target/executable. Precompiling various header files and
                # applying a particular pch to each source file will require custom scripts (as a build phase) and build flags per each
                # file. Since Xcode itself already discourages precompiled headers in favor of modules we don't try much harder here.
                pchs = target.get_pch('c') + target.get_pch('cpp') + target.get_pch('objc') + target.get_pch('objcpp')
                # Make sure to use headers (other backends require implementation files like *.c *.cpp, etc; these should not be used here)
                pchs = [pch for pch in pchs if pch.endswith('.h') or pch.endswith('.hh') or pch.endswith('hpp')]
                if pchs:
                    if len(pchs) > 1:
                        mlog.warning('Unsupported Xcode configuration: More than 1 precompiled header found "{}". Target "{}" might not compile correctly.'.format(str(pchs), target.name))
                    relative_pch_path = os.path.join(target.get_subdir(), pchs[0]) # Path relative to target so it can be used with "$(PROJECT_DIR)"
                    settings_dict.add_item('GCC_PRECOMPILE_PREFIX_HEADER', 'YES')
                    settings_dict.add_item('GCC_PREFIX_HEADER', f'"$(PROJECT_DIR)/{relative_pch_path}"')
            settings_dict.add_item('GCC_PREPROCESSOR_DEFINITIONS', '""')
            settings_dict.add_item('GCC_SYMBOLS_PRIVATE_EXTERN', 'NO')
            if headerdirs:
                header_arr = PbxArray()
                for i in headerdirs:
                    i = os.path.normpath(i)
                    header_arr.add_item(f'"\\"{i}\\""')
                settings_dict.add_item('HEADER_SEARCH_PATHS', header_arr)
            settings_dict.add_item('INSTALL_PATH', f'"{install_path}"')
            settings_dict.add_item('LIBRARY_SEARCH_PATHS', '""')
            if isinstance(target, build.SharedLibrary):
                settings_dict.add_item('LIBRARY_STYLE', 'DYNAMIC')
            self.add_otherargs(settings_dict, langargs)
            settings_dict.add_item('OTHER_LDFLAGS', f'"{ldstr}"')
            settings_dict.add_item('OTHER_REZFLAGS', '""')
            settings_dict.add_item('PRODUCT_NAME', product_name)
            settings_dict.add_item('SECTORDER_FLAGS', '""')
            settings_dict.add_item('SYMROOT', f'"{symroot}"')
            settings_dict.add_item('SYSTEM_HEADER_SEARCH_PATHS', '"{}"'.format(self.environment.get_build_dir()))
            settings_dict.add_item('USE_HEADERMAP', 'NO')
            warn_array = PbxArray()
            settings_dict.add_item('WARNING_CFLAGS', warn_array)
            warn_array.add_item('"$(inherited)"')
            bt_dict.add_item('name', buildtype)

    def add_otherargs(self, settings_dict, langargs):
        for langname, args in langargs.items():
            if args:
                # FIXME, proper quoting
                settings_dict.add_item(f'OTHER_{langname}FLAGS', '"' + ' '.join(args) + '"')

    def generate_xc_configurationList(self, objects_dict):
        # FIXME: sort items
        conf_dict = PbxDict()
        objects_dict.add_item(self.project_conflist, conf_dict, f'Build configuration list for PBXProject "{self.build.project_name}"')
        conf_dict.add_item('isa', 'XCConfigurationList')
        confs_arr = PbxArray()
        conf_dict.add_item('buildConfigurations', confs_arr)
        for buildtype in self.buildtypes:
            confs_arr.add_item(self.project_configurations[buildtype], buildtype)
        conf_dict.add_item('defaultConfigurationIsVisible', 0)
        conf_dict.add_item('defaultConfigurationName', self.buildtype)

        # Now the all target
        all_dict = PbxDict()
        objects_dict.add_item(self.all_buildconf_id, all_dict, 'Build configuration list for PBXAggregateTarget "ALL_BUILD"')
        all_dict.add_item('isa', 'XCConfigurationList')
        conf_arr = PbxArray()
        all_dict.add_item('buildConfigurations', conf_arr)
        for buildtype in self.buildtypes:
            conf_arr.add_item(self.buildall_configurations[buildtype], buildtype)
        all_dict.add_item('defaultConfigurationIsVisible', 0)
        all_dict.add_item('defaultConfigurationName', self.buildtype)

        # Test target
        test_dict = PbxDict()
        objects_dict.add_item(self.test_buildconf_id, test_dict, 'Build configuration list for PBXAggregateTarget "RUN_TEST"')
        test_dict.add_item('isa', 'XCConfigurationList')
        conf_arr = PbxArray()
        test_dict.add_item('buildConfigurations', conf_arr)
        for buildtype in self.buildtypes:
            conf_arr.add_item(self.test_configurations[buildtype], buildtype)
        test_dict.add_item('defaultConfigurationIsVisible', 0)
        test_dict.add_item('defaultConfigurationName', self.buildtype)

        for target_name in self.build_targets:
            t_dict = PbxDict()
            listid = self.buildconflistmap[target_name]
            objects_dict.add_item(listid, t_dict, f'Build configuration list for PBXNativeTarget "{target_name}"')
            t_dict.add_item('isa', 'XCConfigurationList')
            conf_arr = PbxArray()
            t_dict.add_item('buildConfigurations', conf_arr)
            idval = self.buildconfmap[target_name][self.buildtype]
            conf_arr.add_item(idval, self.buildtype)
            t_dict.add_item('defaultConfigurationIsVisible', 0)
            t_dict.add_item('defaultConfigurationName', self.buildtype)


    def generate_prefix(self, pbxdict):
        pbxdict.add_item('archiveVersion', '1')
        pbxdict.add_item('classes', PbxDict())
        pbxdict.add_item('objectVersion', '46')
        objects_dict = PbxDict()
        pbxdict.add_item('objects', objects_dict)
        
        return objects_dict

    def generate_suffix(self, pbxdict):
        pbxdict.add_item('rootObject', self.project_uid, 'Project object')
