package main

import (
 "bytes"
 "context"
 "encoding/json"
 "errors"
 "io"
 "net/http"
 "os"
 "time"
 maxbot "github.com/max-messenger/max-bot-api-client-go/v2"
)

type boundedClient struct { client *http.Client; used bool; status int }
func (c *boundedClient) Do(req *http.Request) (*http.Response,error) {
 if c.used || req.Method!="GET" || req.URL.Scheme!="https" || req.URL.Host!="platform-api2.max.ru" || req.URL.Path!="/messages" {
  return nil,errors.New("request_not_allowed")
 }
 c.used=true
 response,err:=c.client.Do(req); if err!=nil{return nil,err}; c.status=response.StatusCode
 defer response.Body.Close()
 data,err:=io.ReadAll(io.LimitReader(response.Body,4*1024*1024+1))
 if err!=nil || len(data)>4*1024*1024{return nil,errors.New("response_too_large")}
 response.Body=io.NopCloser(bytes.NewReader(data)); return response,nil
}

func main(){
 if os.Getenv("GITHUB_ACTIONS")!="true" || os.Getenv("RUNNER_ENVIRONMENT")!="github-hosted" || len(os.Args)!=3 {os.Exit(2)}
 raw,err:=os.ReadFile(os.Args[1]); if err!=nil || len(raw)>10000 {os.Exit(2)}
 var job struct { ChatID int64 `json:"chat_id"`; Limit int64 `json:"limit"` }
 if json.Unmarshal(raw,&job)!=nil || job.ChatID==0 || job.Limit<1 || job.Limit>100 {os.Exit(2)}
 result:=map[string]any{"status":"requires_max_bot_token","rows":[]any{}}
 if token:=os.Getenv("ARGUS_MAX_BOT_TOKEN");token!="" {
  transport:=&http.Transport{Proxy:nil}
  client:=&boundedClient{client:&http.Client{Timeout:12*time.Second,Transport:transport,
   CheckRedirect:func(req *http.Request,via []*http.Request)error{return http.ErrUseLastResponse}}}
  api,_:=maxbot.NewApi(token,maxbot.WithBaseURL("https://platform-api2.max.ru"),maxbot.WithHTTPClient(client))
  ctx,cancel:=context.WithTimeout(context.Background(),15*time.Second); defer cancel()
  messages,err:=api.Messages.GetMessages(ctx,job.ChatID,0,0,job.Limit,nil)
  if err==nil {result["status"]="ok";result["rows"]=messages.Messages
  } else if client.status==401 {result["status"]="invalid_max_token"
  } else if client.status==403 {result["status"]="requires_max_channel_admin"
  } else if client.status==429 {result["status"]="http_429"
  } else {result["status"]="max_api_or_tls_error"}
 }
 encoded,_:=json.Marshal(result); if os.WriteFile(os.Args[2],encoded,0600)!=nil {os.Exit(2)}
}
