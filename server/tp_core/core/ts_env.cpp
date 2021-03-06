#include "ts_env.h"
#include "../common/ts_const.h"

TsEnv g_env;

TsEnv::TsEnv()
{}

TsEnv::~TsEnv()
{}

bool TsEnv::init(bool load_config)
{
	EXLOG_LEVEL(EX_LOG_LEVEL_INFO);

	ex_exec_file(m_exec_file);

	m_exec_path = m_exec_file;
	ex_dirname(m_exec_path);

	if(!load_config)
		return true;

	// check development flag file, if exists, run in development mode for trace and debug.
	ex_wstr dev_flag_file = m_exec_path;
	ex_path_join(dev_flag_file, false, L"dev_mode", NULL);

	ex_wstr base_path = m_exec_path;
	ex_wstr log_path;
	ex_wstr conf_file;

	if (ex_is_file_exists(dev_flag_file.c_str()))
	{
		EXLOGW("===== DEVELOPMENT MODE =====\n");

		ex_path_join(base_path, true, L"..", L"..", L"..", L"..", L"server", NULL);

		m_etc_path = base_path;
		ex_path_join(m_etc_path, false, L"share", L"etc", NULL);

		conf_file = m_etc_path;
		ex_path_join(conf_file, false, L"core.ini", NULL);

		m_replay_path = base_path;
		ex_path_join(m_replay_path, false, L"share", L"data", L"replay", NULL);

		log_path = base_path;
		ex_path_join(log_path, false, L"share", L"log", NULL);
	}
	else	// not in development mode
	{
#ifdef EX_OS_WIN32
		base_path = m_exec_path;
		ex_path_join(base_path, true, L"..", NULL);
		m_etc_path = base_path;
		ex_path_join(m_etc_path, false, L"etc", NULL);
		conf_file = m_etc_path;
		ex_path_join(conf_file, false, L"core.ini", NULL);
		m_replay_path = base_path;
		ex_path_join(m_replay_path, false, L"data", L"replay", NULL);
		log_path = base_path;
		ex_path_join(log_path, false, L"log", NULL);
#else
		m_etc_path = L"/etc/teleport";
		conf_file = L"/etc/teleport/core.ini";
		m_replay_path = L"/var/lib/teleport/replay";
		log_path = L"/var/log/teleport";
#endif
	}

	//EXLOGW(L"[core] load config file: %ls.\n", conf_file.c_str());

	if (!m_ini.LoadFromFile(conf_file))
	{
		EXLOGE(L"[core] can not load %ls.\n", conf_file.c_str());
		return false;
	}

	ExIniSection* ps = m_ini.GetSection(L"common");

	ex_wstr replay_path;
	if (ps->GetStr(L"replay-path", replay_path))
	{
		m_replay_path = replay_path;
	}

	ex_wstr log_file;
	if (!ps->GetStr(L"log-file", log_file))
	{
		EXLOG_FILE(L"tpcore.log", log_path.c_str());
	}
	else
	{
		ex_remove_white_space(log_file);
		if (log_file[0] == L'"' || log_file[0] == L'\'')
			log_file.erase(0, 1);
		if (log_file[ log_file.length() - 1 ] == L'"' || log_file[log_file.length() - 1] == L'\'')
			log_file.erase(log_file.length() - 1, 1);

		log_path = log_file;
		ex_dirname(log_path);
		ex_wstr file_name;
		file_name.assign(log_file, log_path.length() + 1, log_file.length());

		EXLOG_FILE(file_name.c_str(), log_path.c_str());
	}

	int log_level = EX_LOG_LEVEL_INFO;
	if (ps->GetInt(L"log-level", log_level))
	{
		EXLOG_LEVEL(log_level);
	}

	int debug_mode = 0;
	ps->GetInt(L"debug", debug_mode, 0);
	if (debug_mode == 1)
		EXLOG_DEBUG(true);

	ex_wstr tmp;
	ps = m_ini.GetSection(L"rpc");
	if (!ps->GetStr(L"bind-ip", tmp))
	{
		rpc_bind_ip = TS_HTTP_RPC_HOST;
	}
	else
	{
		ex_wstr2astr(tmp, rpc_bind_ip);
	}
	if (!ps->GetInt(L"bind-port", rpc_bind_port))
	{
		rpc_bind_port = TS_HTTP_RPC_PORT;
	}

	return true;
}
