package main
import (
 "context"
 "io"
 "net/http"
 "strings"
 "testing"
 maxbot "github.com/max-messenger/max-bot-api-client-go/v2"
)
type fixtureClient struct{ t *testing.T; calls int }
func (c *fixtureClient) Do(r *http.Request)(*http.Response,error){
 c.calls++
 if r.Method!="GET" || r.URL.Path!="/messages" || r.URL.Query().Get("chat_id")!="-7" || r.URL.Query().Get("count")!="100" || r.Header.Get("Authorization")!="fixture-token" {c.t.Fatal("unexpected SDK request")}
 return &http.Response{StatusCode:200,Body:io.NopCloser(strings.NewReader(`{"messages":[{"recipient":{"chat_id":-7,"chat_type":"channel"},"timestamp":1793361600000,"body":{"mid":"test","text":"Публікація"},"url":"https://max.ru/c/-7/test"}]}`))},nil
}
func TestReadOnlySDK(t *testing.T){
 client:=&fixtureClient{t:t}; api,_:=maxbot.NewApi("fixture-token",maxbot.WithHTTPClient(client))
 result,err:=api.Messages.GetMessages(context.Background(),-7,0,0,100,nil)
 if err!=nil || len(result.Messages)!=1 || result.Messages[0].Body.Text!="Публікація" || client.calls!=1 {t.Fatal("SDK parsing failed")}
}
func TestTransportRejectsWritesAndOtherDestinations(t *testing.T){
 c:=&boundedClient{}
 for _,target:=range []string{"https://example.com/messages","http://platform-api2.max.ru/messages","https://platform-api2.max.ru/me"}{
  r,_:=http.NewRequest("GET",target,nil);if _,err:=c.Do(r);err==nil {t.Fatal("unexpected destination accepted")}
 }
 r,_:=http.NewRequest("POST","https://platform-api2.max.ru/messages",nil);if _,err:=c.Do(r);err==nil {t.Fatal("write accepted")}
}
