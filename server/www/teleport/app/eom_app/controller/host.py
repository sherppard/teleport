# -*- coding: utf-8 -*-

import time
import csv
import os
import json
import threading
import tornado.gen
import tornado.httpclient

from eom_app.app.configs import app_cfg
from eom_app.app.util import *
from eom_app.module import host
from eom_common.eomcore.logger import *
from eom_app.app.session import web_session
from .base import TPBaseUserAuthHandler, TPBaseAdminAuthHandler, TPBaseUserAuthJsonHandler, TPBaseAdminAuthJsonHandler

cfg = app_cfg()

# 临时认证ID的基数，每次使用时均递减
tmp_auth_id_base = -1
tmp_auth_id_lock = threading.RLock()


class IndexHandler(TPBaseUserAuthHandler):
    def get(self):
        _user = self.get_session('user')
        if _user is None:
            return self.write(-1)

        param = dict()

        param['core'] = {
            'ssh_port': cfg.core.ssh.port,
            'rdp_port': cfg.core.rdp.port,
            'telnet_port': cfg.core.telnet.port
        }

        param['group_list'] = host.get_group_list()

        if _user['type'] >= 100:
            param['cert_list'] = host.get_cert_list()
            self.render('host/admin_index.mako', page_param=json.dumps(param))
        else:
            self.render('host/common_index.mako', page_param=json.dumps(param))


class UploadAndImportHandler(TPBaseAdminAuthJsonHandler):
    # TODO: 导入操作可能会比较耗时，应该分离导入和获取导入状态两个过程，在页面上可以呈现导入进度，并列出导出成功/失败的项

    @tornado.gen.coroutine
    def post(self):
        """
        csv导入规则：
        每一行的数据格式：  分组ID,操作系统,IP地址,端口,系统用户,系统密码,协议,密钥ID,状态,认证类型,描述
        因为主机的唯一性在于 `IP地址 + 端口`，且允许一个 `IP地址 + 端口` 对应多个系统用户，因此，每一行的数据几乎没有限制。
        在导入时：
          1. 对每一个第一次遇到的 `IP地址 + 端口` 组合，就在 ts_host_info 表中加一个条目，并在 ts_auth_info 表中加入一个用户。
          2. 对于非第一次遇到的 `IP地址 + 端口` 组合，则仅仅在 ts_auth_info 表中加一个用户，不更改 ts_host_info 表中的现有数据。
          3. `IP地址 + 端口 + 用户` 的组合不能重复。
          4. 空行跳过，数据格式不正确的跳过。
        """
        ret = dict()
        ret['code'] = 0
        ret['msg'] = list()  # 记录跳过的行（格式不正确，或者数据重复等）
        csv_filename = ''

        try:
            # upload_path = os.path.join(os.path.dirname(__file__), 'csv-files')  # 文件的暂存路径
            upload_path = os.path.join(cfg.data_path, 'tmp')  # 文件的暂存路径
            if not os.path.exists(upload_path):
                os.mkdir(upload_path)
            file_metas = self.request.files['csvfile']  # 提取表单中‘name’为‘file’的文件元数据
            for meta in file_metas:
                now = time.localtime(time.time())
                tmp_name = 'upload-{:04d}{:02d}{:02d}{:02d}{:02d}{:02d}.csv'.format(now.tm_year, now.tm_mon, now.tm_mday, now.tm_hour, now.tm_min, now.tm_sec)
                csv_filename = os.path.join(upload_path, tmp_name)
                with open(csv_filename, 'wb') as up:
                    up.write(meta['body'])

            # file encode maybe utf8 or gbk... check it out.
            file_encode = None
            with open(csv_filename, encoding='gbk') as up:
                try:
                    up.readlines()
                    file_encode = 'gbk'
                except:
                    log.e('open file:{} -1\n'.format(csv_filename))

            if file_encode is None:
                with open(csv_filename, encoding='utf8') as up:
                    try:
                        up.readlines()
                        file_encode = 'utf8'
                    except:
                        log.e('open file:{} -2\n'.format(csv_filename))

            if file_encode is None:
                os.remove(csv_filename)
                self.write_json(-2)
                log.e('file {} unknown encode.\n'.format(csv_filename))
                return

            with open(csv_filename, encoding=file_encode) as up:
                csv_reader = csv.reader(up)
                is_first_line = True
                for csv_recorder in csv_reader:
                    # 跳过第一行，那是格式说明
                    if is_first_line:
                        is_first_line = False
                        continue

                    # 空行则忽略
                    if len(csv_recorder) <= 1:
                        continue

                    # 格式错误则记录在案，然后继续
                    if len(csv_recorder) != 13:
                        ret['msg'].append({'reason': '格式错误', 'line': ', '.join(csv_recorder)})
                        continue

                    # pro_type = int(line[6])
                    # host_port = int(line[3])

                    host_args = dict()
                    user_args = dict()
                    # 分组ID, 操作系统, IP地址, 端口, 协议, 状态, 描述, 系统用户, 系统密码, 是否加密,附加参数,  密钥ID, 认证类型

                    host_args['group_id'] = int(csv_recorder[0])
                    host_args['host_sys_type'] = int(csv_recorder[1])
                    host_args['host_ip'] = csv_recorder[2]
                    host_args['host_port'] = csv_recorder[3]
                    host_args['protocol'] = csv_recorder[4]
                    host_args['host_lock'] = csv_recorder[5]
                    host_args['host_desc'] = csv_recorder[6]
                    # 加入一个主机（如果已经存在，则直接返回已存在的条目的host_id）
                    host_id = host.add_host(host_args, must_not_exists=False)
                    if host_id < 0:
                        ret['msg'].append({'reason': '添加主机失败，操作数据库失败', 'line': ', '.join(csv_recorder)})
                        continue

                    user_args['host_id'] = host_id
                    user_args['user_name'] = csv_recorder[7]
                    user_pswd = csv_recorder[8]
                    is_encrpty = int(csv_recorder[9])
                    user_args['user_param'] = csv_recorder[10].replace('\\n', '\n')
                    user_args['cert_id'] = int(csv_recorder[11])
                    auth_mode = int(csv_recorder[12])
                    user_args['auth_mode'] = auth_mode
                    user_args['user_pswd'] = ''
                    ret_code = 0
                    if auth_mode == 0:
                        pass
                    elif auth_mode == 1:
                        try:
                            if is_encrpty == 0:
                                # ret_code, tmp_pswd = get_enc_data(user_pswd)
                                _yr = async_enc(user_pswd)
                                return_data = yield _yr
                                if return_data is None:
                                    return self.write_json(-1)

                                if 'code' not in return_data or return_data['code'] != 0:
                                    return self.write_json(-1)

                                tmp_pswd = return_data['data']

                            else:
                                tmp_pswd = user_pswd

                            user_args['user_pswd'] = tmp_pswd

                        except Exception:
                            ret_code = -1
                            log.e('get_enc_data() failed.\n')

                        if 0 != ret_code:
                            ret['msg'].append({'reason': '加密用户密码失败，可能原因：Teleport核心服务未启动', 'line': ', '.join(csv_recorder)})
                            log.e('get_enc_data() failed, error={}\n'.format(ret_code))
                            continue

                    elif auth_mode == 2:
                        pass
                        # user_args['cert_id'] = int(csv_recorder[7])
                    else:
                        ret['msg'].append({'reason': '未知的认证模式', 'line': ', '.join(csv_recorder)})
                        log.e('auth_mode unknown\n')
                        continue

                    uid = host.sys_user_add(user_args)
                    if uid < 0:
                        if uid == -100:
                            ret['msg'].append({'reason': '添加登录账号失败，账号已存在', 'line': ', '.join(csv_recorder)})
                        else:
                            ret['msg'].append({'reason': '添加登录账号失败，操作数据库失败', 'line': ', '.join(csv_recorder)})
                            # log.e('sys_user_add() failed.\n')

            ret = json.dumps(ret).encode('utf8')
            self.write(ret)
        except:
            log.e('error\n')
            ret['code'] = -1
            ret = json.dumps(ret).encode('utf8')
            self.write(ret)

        finally:
            if os.path.exists(csv_filename):
                os.remove(csv_filename)


class GetListHandler(TPBaseUserAuthJsonHandler):
    def post(self):
        _user = self.get_session('user')
        if _user is None:
            return self.write(-1)

        _type = _user['type']
        _uname = _user['name']

        filter = dict()
        user = self.get_current_user()
        order = dict()
        order['name'] = 'host_id'
        order['asc'] = True
        limit = dict()
        limit['page_index'] = 0
        limit['per_page'] = 25

        args = self.get_argument('args', None)
        if args is not None:
            args = json.loads(args)

            tmp = list()
            _filter = args['filter']
            for i in _filter:
                if i == 'host_sys_type' and _filter[i] == 0:
                    tmp.append(i)
                    continue
                if i == 'host_group' and _filter[i] == 0:
                    tmp.append(i)
                    continue
                if i == 'search':
                    _x = _filter[i].strip()
                    if len(_x) == 0:
                        tmp.append(i)
                    continue

            for i in tmp:
                del _filter[i]

            filter.update(_filter)

            _limit = args['limit']
            if _limit['page_index'] < 0:
                _limit['page_index'] = 0
            if _limit['per_page'] < 10:
                _limit['per_page'] = 10
            if _limit['per_page'] > 100:
                _limit['per_page'] = 100

            limit.update(_limit)

            _order = args['order']
            if _order is not None:
                order['name'] = _order['k']
                order['asc'] = _order['v']
        if _type == 100:
            _total, _hosts = host.get_all_host_info_list(filter, order, limit)
        else:
            filter['account_name'] = _uname
            _total, _hosts = host.get_host_info_list_by_user(filter, order, limit)
        # print(_hosts)

        ret = dict()
        ret['page_index'] = limit['page_index']
        ret['total'] = _total
        ret['data'] = _hosts
        self.write_json(0, data=ret)
        # self.write(json_encode(data))


class GetGrouplist(TPBaseUserAuthJsonHandler):
    def post(self):
        group_list = host.get_group_list()
        self.write_json(0, data=group_list)


class UpdateHandler(TPBaseUserAuthJsonHandler):
    def post(self):
        args = self.get_argument('args', None)
        if args is not None:
            args = json.loads(args)
            # print('args', args)
        else:
            # ret = {'code':-1}
            self.write_json(-1)
            return

        if 'host_id' not in args or 'kv' not in args:
            # ret = {'code':-2}
            self.write_json(-2)
            return

        # _host_id = args['host_id']

        _ret = host.update(args['host_id'], args['kv'])

        if _ret:
            self.write_json(0)
        else:
            self.write_json(-1)


class AddHost(TPBaseUserAuthJsonHandler):
    def post(self):
        args = self.get_argument('args', None)
        if args is not None:
            args = json.loads(args)
            # print('args', args)
        else:
            # ret = {'code':-1}
            self.write_json(-1)
            return

        try:
            ret = host.add_host(args)
            if ret > 0:
                self.write_json(0)
            else:
                self.write_json(ret)
            return
        except:
            self.write_json(-1)
            return


class LockHost(TPBaseUserAuthJsonHandler):
    def post(self):
        args = self.get_argument('args', None)
        if args is not None:
            args = json.loads(args)
            # print('args', args)
        else:
            # ret = {'code':-1}
            self.write_json(-1)
            return

        host_id = args['host_id']
        lock = args['lock']
        try:
            ret = host.lock_host(host_id, lock)
            if ret:
                self.write_json(0)
            else:
                self.write_json(-1)
            return
        except:
            self.write_json(-1)
            return


class DeleteHost(TPBaseUserAuthJsonHandler):
    def post(self):
        args = self.get_argument('args', None)
        if args is not None:
            args = json.loads(args)
            # print('args', args)
        else:
            # ret = {'code':-1}
            self.write_json(-1)
            return
        host_list = args['host_list']
        try:
            ret = host.delete_host(host_list)
            if ret:
                self.write_json(0)
            else:
                self.write_json(-1)
            return
        except:
            self.write_json(-1)
            return


class ExportHostHandler(TPBaseAdminAuthHandler):
    def get(self):
        self.set_header('Content-Type', 'application/octet-stream')
        self.set_header('Content-Disposition', 'attachment; filename=teleport-host-export.csv')

        order = dict()
        order['name'] = 'host_id'
        order['asc'] = True
        limit = dict()
        limit['page_index'] = 0
        limit['per_page'] = 999999
        _total, _hosts = host.get_all_host_info_list(dict(), order, limit, True)

        self.write("分组ID, 操作系统, IP地址, 端口, 协议, 状态, 描述, 系统用户, 系统密码, 是否加密, 附加参数, 密钥ID, 认证类型\n".encode('gbk'))

        try:

            for h in _hosts:
                auth_list = h['auth_list']
                # 分组ID, 操作系统, IP地址, 端口, 协议, 状态, 描述, 系统用户, 系统密码, 是否加密,附加参数, 密钥ID, 认证类型
                for j in auth_list:
                    row_string = ''
                    # row_string = str(h['host_id'])
                    # row_string += ','
                    row_string += str(h['group_id'])
                    row_string += ','
                    row_string += str(h['host_sys_type'])
                    row_string += ','
                    row_string += h['host_ip']
                    row_string += ','
                    row_string += str(h['host_port'])
                    row_string += ','
                    row_string += str(h['protocol'])
                    row_string += ','
                    row_string += str(h['host_lock'])
                    row_string += ','
                    row_string += h['host_desc']
                    row_string += ','

                    # row_string += str(j['host_auth_id'])
                    # row_string += ','
                    row_string += j['user_name']
                    row_string += ','
                    row_string += j['user_pswd']
                    row_string += ','
                    row_string += '1'
                    row_string += ','
                    user_param = j['user_param']
                    if len(user_param) > 0:
                        user_param = user_param.replace('\n', '\\n')
                        row_string += user_param
                    row_string += ','
                    row_string += str(j['cert_id'])
                    row_string += ','
                    row_string += str(j['auth_mode'])

                    self.write(row_string.encode('gbk'))
                    self.write('\n')

        except IndexError:
            self.write('**********************************************\n'.encode('gbk'))
            self.write('！！错误！！\n'.encode('gbk'))
            self.write('导出过程中发生了错误！！\n'.encode('gbk'))
            self.write('**********************************************\n'.encode('gbk'))
            log.e('')

        self.finish()


class GetCertList(TPBaseUserAuthJsonHandler):
    def post(self):
        _certs = host.get_cert_list()
        if _certs is None or len(_certs) == 0:
            self.write_json(-1)
            return
        else:
            self.write_json(0, data=_certs)
            return


class AddCert(TPBaseUserAuthJsonHandler):
    @tornado.gen.coroutine
    def post(self):
        args = self.get_argument('args', None)
        if args is not None:
            args = json.loads(args)
        else:
            self.write_json(-1)
            return

        cert_pub = args['cert_pub']
        cert_pri = args['cert_pri']
        cert_name = args['cert_name']

        if len(cert_pri) == 0:
            self.write_json(-1)
            return

        _yr = async_enc(cert_pri)
        return_data = yield _yr
        if return_data is None:
            return self.write_json(-1)

        if 'code' not in return_data or return_data['code'] != 0:
            return self.write_json(-1)

        cert_pri = return_data['data']

        try:
            ret = host.add_cert(cert_pub, cert_pri, cert_name)
            if ret:
                return self.write_json(0)
            else:
                return self.write_json(-1)
        except:
            return self.write_json(-1)


class DeleteCert(TPBaseUserAuthJsonHandler):
    def post(self):
        args = self.get_argument('args', None)
        if args is not None:
            args = json.loads(args)
        else:
            return self.write_json(-1)

        cert_id = args['cert_id']

        try:
            ret = host.delete_cert(cert_id)
            if ret:
                return self.write_json(0)
            else:
                return self.write_json(-2)
        except:
            return self.write_json(-3)


class UpdateCert(TPBaseUserAuthJsonHandler):
    @tornado.gen.coroutine
    def post(self):
        args = self.get_argument('args', None)
        if args is not None:
            args = json.loads(args)
            # print('args', args)
        else:
            # ret = {'code':-1}
            self.write_json(-1)
            return
        cert_id = args['cert_id']
        cert_pub = args['cert_pub']
        cert_pri = args['cert_pri']
        cert_name = args['cert_name']

        if len(cert_pri) > 0:
            _yr = async_enc(cert_pri)
            return_data = yield _yr
            if return_data is None:
                return self.write_json(-1)

            if 'code' not in return_data or return_data['code'] != 0:
                return self.write_json(-1)

            cert_pri = return_data['data']

        try:
            ret = host.update_cert(cert_id, cert_pub, cert_pri, cert_name)
            if ret:
                self.write_json(0)
            else:
                self.write_json(-1)
            return
        except:
            self.write_json(-1)
            return


class AddGroup(TPBaseUserAuthJsonHandler):
    def post(self):
        args = self.get_argument('args', None)
        if args is not None:
            args = json.loads(args)
            # print('args', args)
        else:
            # ret = {'code':-1}
            self.write_json(-1)
            return
        group_name = args['group_name']
        try:
            ret = host.add_group(group_name)
            if ret:
                self.write_json(0)
            else:
                self.write_json(-1)
            return
        except:
            self.write_json(-1)
            return


class UpdateGroup(TPBaseUserAuthJsonHandler):
    def post(self):
        args = self.get_argument('args', None)
        if args is not None:
            args = json.loads(args)
            # print('args', args)
        else:
            # ret = {'code':-1}
            self.write_json(-1)
            return
        group_id = args['group_id']
        group_name = args['group_name']
        try:
            ret = host.update_group(group_id, group_name)
            if ret:
                self.write_json(0)
            else:
                self.write_json(-1)
            return
        except:
            self.write_json(-1)
            return


class DeleteGroup(TPBaseUserAuthJsonHandler):
    def post(self):
        args = self.get_argument('args', None)
        if args is not None:
            args = json.loads(args)
            # print('args', args)
        else:
            # ret = {'code':-1}
            self.write_json(-1)
            return
        group_id = args['group_id']
        try:
            ret = host.delete_group(group_id)
            if ret == 0:
                self.write_json(0)
            else:
                self.write_json(ret)
            return
        except:
            self.write_json(-1)
            return


class AddHostToGroup(TPBaseUserAuthJsonHandler):
    def post(self):
        args = self.get_argument('args', None)
        if args is not None:
            args = json.loads(args)
            # print('args', args)
        else:
            # ret = {'code':-1}
            self.write_json(-1)
            return
        host_list = args['host_list']
        group_id = args['group_id']
        try:
            ret = host.add_host_to_group(host_list, group_id)
            if ret:
                self.write_json(0)
            else:
                self.write_json(-1)
            return
        except:
            self.write_json(-1)
            return


class GetSessionId(TPBaseUserAuthJsonHandler):
    @tornado.gen.coroutine
    def post(self, *args, **kwargs):
        args = self.get_argument('args', None)
        if args is not None:
            args = json.loads(args)
            # print('args', args)
        else:
            # ret = {'code':-1}
            self.write_json(-1)
            return
        if 'auth_id' not in args:
            self.write_json(-1)
            return
        auth_id = args['auth_id']

        req = {'method': 'request_session', 'param': {'authid': auth_id}}
        _yr = async_post_http(req)
        return_data = yield _yr
        if return_data is None:
            return self.write_json(-1)

        if 'code' not in return_data:
            return self.write_json(-1)

        _code = return_data['code']
        if _code != 0:
            return self.write_json(_code)

        try:
            session_id = return_data['data']['sid']
        except IndexError:
            return self.write_json(-1)

        data = dict()
        data['session_id'] = session_id

        return self.write_json(0, data=data)


class AdminGetSessionId(TPBaseUserAuthJsonHandler):
    @tornado.gen.coroutine
    def post(self, *args, **kwargs):
        args = self.get_argument('args', None)
        if args is not None:
            args = json.loads(args)
        else:
            self.write_json(-1)
            return

        if 'host_auth_id' not in args:
            self.write_json(-1)
            return

        _host_auth_id = int(args['host_auth_id'])

        user = self.get_current_user()

        # host_auth_id 对应的是 ts_auth_info 表中的某个条目，含有具体的认证数据，因为管理员无需授权即可访问所有远程主机，因此
        # 直接给出 host_auth_id，且account直接指明是当前登录用户（其必然是管理员）

        tmp_auth_info = host.get_host_auth_info(_host_auth_id)
        if tmp_auth_info is None:
            self.write_json(-1)
            return

        tmp_auth_info['account_lock'] = 0
        tmp_auth_info['account_name'] = user['name']

        with tmp_auth_id_lock:
            global tmp_auth_id_base
            tmp_auth_id_base -= 1
            auth_id = tmp_auth_id_base

        # 将这个临时认证信息放到session中备后续查找使用（10秒内有效）
        web_session().set('tmp-auth-info-{}'.format(auth_id), tmp_auth_info, 10)

        req = {'method': 'request_session', 'param': {'authid': auth_id}}
        _yr = async_post_http(req)
        return_data = yield _yr
        if return_data is None:
            return self.write_json(-1)

        if 'code' not in return_data:
            return self.write_json(-1)

        _code = return_data['code']
        if _code != 0:
            return self.write_json(_code)

        try:
            session_id = return_data['data']['sid']
        except IndexError:
            return self.write_json(-1)

        data = dict()
        data['session_id'] = session_id

        return self.write_json(0, data=data)


class AdminFastGetSessionId(TPBaseAdminAuthJsonHandler):
    @tornado.gen.coroutine
    def post(self, *args, **kwargs):
        args = self.get_argument('args', None)
        if args is not None:
            args = json.loads(args)
        else:
            self.write_json(-1)
            return

        user = self.get_current_user()

        tmp_auth_info = dict()

        try:
            _host_auth_id = int(args['host_auth_id'])
            _user_pswd = args['user_pswd']
            _cert_id = int(args['cert_id'])

            tmp_auth_info['host_ip'] = args['host_ip']
            tmp_auth_info['host_port'] = int(args['host_port'])
            tmp_auth_info['sys_type'] = int(args['sys_type'])
            tmp_auth_info['protocol'] = int(args['protocol'])
            tmp_auth_info['user_name'] = args['user_name']
            tmp_auth_info['auth_mode'] = int(args['auth_mode'])
            tmp_auth_info['user_param'] = args['user_param']
            tmp_auth_info['encrypt'] = 1
            tmp_auth_info['account_lock'] = 0
            tmp_auth_info['account_name'] = user['name']
        except IndexError:
            self.write_json(-2)
            return

        if tmp_auth_info['auth_mode'] == 1:
            if len(_user_pswd) == 0:  # 修改登录用户信息时可能不会修改密码，因此页面上可能不会传来密码，需要从数据库中直接读取
                h = host.get_host_auth_info(_host_auth_id)
                tmp_auth_info['user_auth'] = h['user_auth']
            else:  # 如果页面上修改了密码或者新建账号时设定了密码，那么需要先交给core服务进行加密
                req = {'method': 'enc', 'param': {'p': _user_pswd}}
                _yr = async_post_http(req)
                return_data = yield _yr
                if return_data is None:
                    return self.write_json(-1)
                if 'code' not in return_data or return_data['code'] != 0:
                    return self.write_json(-1)

                tmp_auth_info['user_auth'] = return_data['data']['c']

        elif tmp_auth_info['auth_mode'] == 2:
            tmp_auth_info['user_auth'] = host.get_cert_info(_cert_id)
            if tmp_auth_info['user_auth'] is None:
                self.write_json(-100)
                return
        elif tmp_auth_info['auth_mode'] == 0:
            tmp_auth_info['user_auth'] = ''
        else:
            self.write_json(-101)
            return

        with tmp_auth_id_lock:
            global tmp_auth_id_base
            tmp_auth_id_base -= 1
            auth_id = tmp_auth_id_base

        web_session().set('tmp-auth-info-{}'.format(auth_id), tmp_auth_info, 10)

        req = {'method': 'request_session', 'param': {'authid': auth_id}}
        _yr = async_post_http(req)
        return_data = yield _yr
        if return_data is None:
            return self.write_json(-1)

        if 'code' not in return_data:
            return self.write_json(-1)

        _code = return_data['code']
        if _code != 0:
            return self.write_json(_code)

        try:
            session_id = return_data['data']['sid']
        except IndexError:
            return self.write_json(-1)

        data = dict()
        data['session_id'] = session_id

        return self.write_json(0, data=data)


class SysUserList(TPBaseUserAuthJsonHandler):
    def post(self, *args, **kwargs):
        args = self.get_argument('args', None)
        if args is not None:
            args = json.loads(args)
        else:
            self.write_json(-1)
            return
        try:
            host_id = args['host_id']
        except Exception as e:
            self.write_json(-2)
            return

        data = host.sys_user_list(host_id)
        return self.write_json(0, data=data)


class SysUserAdd(TPBaseUserAuthJsonHandler):
    @tornado.gen.coroutine
    def post(self, *args, **kwargs):
        args = self.get_argument('args', None)
        if args is not None:
            args = json.loads(args)
        else:
            return self.write_json(-1)

        try:
            auth_mode = args['auth_mode']
            user_pswd = args['user_pswd']
            cert_id = args['cert_id']
        except IndexError:
            return self.write_json(-2)

        if auth_mode == 1:
            if 0 == len(args['user_pswd']):
                return self.write_json(-1)

            _yr = async_enc(user_pswd)
            return_data = yield _yr
            if return_data is None:
                return self.write_json(-1)

            if 'code' not in return_data or return_data['code'] != 0:
                return self.write_json(-1)

            args['user_pswd'] = return_data['data']

        user_id = host.sys_user_add(args)
        if user_id < 0:
            if user_id == -100:
                return self.write_json(user_id, '同名账户已经存在！')
            else:
                return self.write_json(user_id, '数据库操作失败！')

        return self.write_json(0)


class SysUserUpdate(TPBaseUserAuthJsonHandler):
    @tornado.gen.coroutine
    def post(self, *args, **kwargs):
        args = self.get_argument('args', None)
        if args is not None:
            args = json.loads(args)
        else:
            # ret = {'code':-1}
            self.write_json(-1)
            return

        if 'host_auth_id' not in args or 'kv' not in args:
            # ret = {'code':-2}
            self.write_json(-2)
            return

        kv = args['kv']
        if 'auth_mode' not in kv or 'user_pswd' not in kv or 'cert_id' not in kv:
            self.write_json(-3)
            return

        auth_mode = kv['auth_mode']
        if 'user_pswd' in kv:
            user_pswd = kv['user_pswd']
            if 0 == len(user_pswd):
                args['kv'].pop('user_pswd')
                user_pswd = None
        else:
            user_pswd = None

        cert_id = kv['cert_id']
        if auth_mode == 1 and user_pswd is not None:
            _yr = async_enc(user_pswd)
            return_data = yield _yr
            if return_data is None:
                return self.write_json(-1)

            if 'code' not in return_data or return_data['code'] != 0:
                return self.write_json(-1)

            args['kv']['user_pswd'] = return_data['data']

        if host.sys_user_update(args['host_auth_id'], args['kv']):
            return self.write_json(0)

        return self.write_json(-1)


class SysUserDelete(TPBaseUserAuthJsonHandler):
    def post(self, *args, **kwargs):
        args = self.get_argument('args', None)
        if args is not None:
            args = json.loads(args)
        else:
            self.write_json(-2)
            return
        try:
            host_auth_id = args['host_auth_id']
        except IndexError:
            self.write_json(-2)
            return

        if host.sys_user_delete(host_auth_id):
            return self.write_json(0)

        return self.write_json(-1)
