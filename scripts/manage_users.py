"""Manage deployment accounts locally; credentials are read without echo."""
import argparse
import getpass
from app import auth


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('username')
    p.add_argument('--role',choices=('employee','admin'),default='employee')
    p.add_argument('--disable',action='store_true')
    args=p.parse_args()
    token=None if args.disable else getpass.getpass('访问令牌（至少20字符，不回显）：')
    auth.set_user(args.username,token,args.role,not args.disable)
    auth.audit('deployment-cli','disable_user' if args.disable else 'set_user',args.username)
    print('账号配置已保存；访问令牌未写入日志。')


if __name__=='__main__':main()
