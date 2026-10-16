"""python -m app.run --clock manual --planner model --mode live --trace on"""
import argparse
import os
def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--clock',choices=['real','manual'],default='manual')
    parser.add_argument('--planner',choices=['model','legacy'],default='model')
    parser.add_argument('--mode',choices=['live','mock'],default='mock')
    parser.add_argument('--trace',choices=['on','off'],default='on')
    parser.add_argument('--port',type=int,default=8000)
    args=parser.parse_args()
    os.environ.update(FINAGENT_CLOCK=args.clock,FINAGENT_PLANNER=args.planner,
                      FINAGENT_MODE=args.mode,FINAGENT_TRACE=args.trace)
    import uvicorn
    uvicorn.run('app.main:app',host='127.0.0.1',port=args.port,workers=1)
if __name__=='__main__':
    main()
